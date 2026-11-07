"""Source-level multipart streaming quota tests; no database or parser image."""

import asyncio

import pytest
from starlette.datastructures import Headers


def _multipart_body(file_bytes, *, boundary="folio-boundary", close=True):
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="rights_attestation"\r\n\r\n'
        "authored-synthetic\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="document_roles"\r\n\r\n'
        "invoice\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="invoice.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode() if close else b""
    return prefix + file_bytes + suffix


def _parser(body, *, boundary="folio-boundary", chunk_size=65536):
    from folio.workbench import BoundedIntakeMultiPartParser

    async def stream():
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]

    parser = BoundedIntakeMultiPartParser(
        headers=Headers(
            {
                "content-type": f"multipart/form-data; boundary={boundary}",
            }
        ),
        stream=stream(),
        max_files=10,
        max_fields=12,
        max_part_size=64 * 1024,
    )
    return parser


def test_multipart_exact_twenty_mib_file_passes_across_stream_chunk_boundaries():
    from folio.intake_limits import MAX_FILE_BYTES

    parser = _parser(_multipart_body(b"x" * MAX_FILE_BYTES), chunk_size=65531)

    async def run():
        form = await parser.parse()
        try:
            upload = form.get("files")
            assert upload.size == MAX_FILE_BYTES
            assert upload.file.seek(0, 2) == MAX_FILE_BYTES
        finally:
            await form.close()

    assert MAX_FILE_BYTES == 20 * 1024 * 1024
    asyncio.run(run())


def test_multipart_one_byte_over_twenty_mib_fails_before_spooling_excess_and_closes_files():
    from folio.intake_limits import MAX_FILE_BYTES
    from folio.workbench import IntakeFilePartLimit

    parser = _parser(_multipart_body(b"x" * (MAX_FILE_BYTES + 1)), chunk_size=65521)
    with pytest.raises(IntakeFilePartLimit, match="FILE_LIMIT"):
        asyncio.run(parser.parse())
    assert parser._files_to_close_on_error
    assert all(spooled.closed for spooled in parser._files_to_close_on_error)


def test_malformed_multipart_closes_any_created_file_spools():
    from starlette.formparsers import MultiPartException

    parser = _parser(
        _multipart_body(b"small authored payload", close=False), chunk_size=7
    )
    with pytest.raises(MultiPartException):
        asyncio.run(parser.parse())
    assert parser._files_to_close_on_error
    assert all(spooled.closed for spooled in parser._files_to_close_on_error)


def test_multipart_valid_final_boundary_split_across_small_chunks():
    parser = _parser(
        _multipart_body(b"small authored payload"), chunk_size=3
    )

    async def run():
        form = await parser.parse()
        try:
            upload = form.get("files")
            assert upload is not None
            assert upload.file.read() == b"small authored payload"
        finally:
            await form.close()

    asyncio.run(run())


def test_multipart_malformed_final_terminator_closes_created_file_spools():
    from starlette.formparsers import MultiPartException

    body = _multipart_body(b"small authored payload").replace(
        b"--folio-boundary--\r\n", b"--folio-boundary-!\r\n"
    )
    parser = _parser(body, chunk_size=5)
    with pytest.raises(MultiPartException):
        asyncio.run(parser.parse())
    assert parser._files_to_close_on_error
    assert all(spooled.closed for spooled in parser._files_to_close_on_error)

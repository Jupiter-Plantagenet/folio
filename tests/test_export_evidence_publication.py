"""Regression controls for the public/private proof-artifact boundary."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

# The evidence publisher is Windows-host tooling: it accepts only C:-anchored paths.
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-host evidence publisher (C:-anchored)"
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import secret_scan  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "export_proof", ROOT / "scripts/export_proof.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_private_subtrees_are_never_copied_even_when_scanner_skips_them(tmp_path):
    source = tmp_path / "private-output"
    (source / ".local").mkdir(parents=True)
    (source / ".local/private.txt").write_text("private fixture sentinel")
    (source / "result.json").write_text('{"verified":true}')
    destination = tmp_path / "evidence"
    module.publish_outputs(source, destination)
    assert (destination / "result.json").read_text() == '{"verified":true}'
    assert not (destination / ".local").exists()


def test_secret_pattern_withholds_all_raw_outputs(tmp_path):
    source = tmp_path / "private-output"
    source.mkdir()
    # Generated throwaway fixture only, never an operational credential.
    fixture_secret = "postgresql://fixture:fixture-only-secret@localhost/db"
    secret_name = "ghp_" + "A" * 36 + ".log"
    (source / secret_name).write_text(fixture_secret)
    (source / "clean.json").write_text('{"verified":true}')
    destination = tmp_path / "evidence"
    with pytest.raises(SystemExit, match="TEST_OUTPUT_NOT_CERTIFIED"):
        module.publish_outputs(source, destination)
    assert not (destination / secret_name).exists()
    assert not (destination / "clean.json").exists()
    report = (destination / "outputs-withheld.json").read_text(encoding="utf-8")
    expected_hash = hashlib.sha256(fixture_secret.encode("utf-8")).hexdigest()
    assert secret_name not in report
    assert expected_hash not in report
    assert "match_sha256" not in report
    assert '"path"' not in report
    assert '"reason": "TEST_OUTPUT_NOT_CERTIFIED_FOR_PUBLICATION"' in report
    assert '"dsn_with_password": 1' in report


def test_oversized_unscanned_output_stays_private(tmp_path):
    source = tmp_path / "private-output"
    source.mkdir()
    (source / "huge.log").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    destination = tmp_path / "evidence"
    with pytest.raises(SystemExit, match="TEST_OUTPUT_NOT_CERTIFIED"):
        module.publish_outputs(source, destination)
    assert not (destination / "huge.log").exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"invalid utf8 byte: \xff",
        "postgresql://fixture:fixture-only-secret@localhost/db".encode("utf-16le"),
    ],
)
def test_non_utf8_or_binary_output_is_withheld(tmp_path, payload):
    source = tmp_path / "private-output"
    source.mkdir()
    (source / "result.log").write_bytes(payload)
    destination = tmp_path / "evidence"
    with pytest.raises(SystemExit, match="TEST_OUTPUT_NOT_CERTIFIED"):
        module.publish_outputs(source, destination)
    assert not (destination / "result.log").exists()
    report = (destination / "outputs-withheld.json").read_text(encoding="utf-8")
    assert "OUTPUT_NOT_STRICT_UTF8" in report or "OUTPUT_CONTAINS_NUL" in report
    assert "fixture-only-secret" not in report


def test_unsupported_format_is_not_certified_clean(tmp_path):
    source = tmp_path / "private-output"
    source.mkdir()
    (source / "opaque.bin").write_bytes(b"ascii text is not a format policy")
    destination = tmp_path / "evidence"
    with pytest.raises(SystemExit, match="TEST_OUTPUT_NOT_CERTIFIED"):
        module.publish_outputs(source, destination)
    report = (destination / "outputs-withheld.json").read_text(encoding="utf-8")
    assert "OUTPUT_FORMAT_UNSUPPORTED" in report
    assert not (destination / "opaque.bin").exists()


def test_same_inode_growth_cannot_change_published_scanned_bytes(tmp_path, monkeypatch):
    source = tmp_path / "private-output"
    source.mkdir()
    original = b'{"state":"clean"}'
    item = source / "result.json"
    item.write_bytes(original)
    inode_before = secret_scan.os.lstat(item).st_ino
    real_read = secret_scan.read_regular_file

    def read_then_grow(path, *, max_bytes=secret_scan.MAX_BYTES):
        scanned = real_read(path, max_bytes=max_bytes)
        with Path(path).open("ab") as stream:
            stream.write(
                b" postgresql://fixture:fixture-only-secret@localhost/db"
            )
        return scanned

    monkeypatch.setattr(secret_scan, "read_regular_file", read_then_grow)
    destination = tmp_path / "evidence"
    module.publish_outputs(source, destination)
    assert secret_scan.os.lstat(item).st_ino == inode_before
    assert item.stat().st_size > len(original)
    assert (destination / "result.json").read_bytes() == original


def test_batch_payload_memory_limit_withholds_all_outputs(tmp_path, monkeypatch):
    source = tmp_path / "private-output"
    source.mkdir()
    (source / "a.txt").write_text("ab")
    (source / "b.txt").write_text("cd")
    monkeypatch.setattr(module, "MAX_PUBLIC_BATCH_BYTES", 3)
    destination = tmp_path / "evidence"
    with pytest.raises(SystemExit, match="TEST_OUTPUT_NOT_CERTIFIED"):
        module.publish_outputs(source, destination)
    assert not (destination / "a.txt").exists()
    assert not (destination / "b.txt").exists()
    assert "OUTPUT_BATCH_EXCEEDS_PUBLICATION_MEMORY_LIMIT" in (
        destination / "outputs-withheld.json"
    ).read_text(encoding="utf-8")


def test_reparse_tree_is_refused_without_publishing(tmp_path):
    source = tmp_path / "private-output"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("fixture-only sentinel")
    link = source / "linked-directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable in this environment")
    destination = tmp_path / "evidence"
    with pytest.raises(secret_scan.UnsafeScanPath, match="REPARSE_POINT"):
        module.publish_outputs(source, destination)
    assert not destination.exists()


def test_scanner_rejects_reparse_ancestor_before_enumeration(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "fixture.txt").write_text("not a credential")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable in this environment")
    calls = []
    original_lstat = secret_scan.os.lstat

    def record_lstat(path):
        calls.append(Path(path))
        return original_lstat(path)

    monkeypatch.setattr(secret_scan.os, "lstat", record_lstat)
    monkeypatch.setattr(
        Path,
        "exists",
        lambda _path: pytest.fail("ancestry validation must not call Path.exists"),
    )
    with pytest.raises(secret_scan.UnsafeScanPath, match="REPARSE_POINT"):
        secret_scan.validate_c_ancestry(alias / "must-not-be-probed")
    assert alias in calls
    assert alias / "must-not-be-probed" not in calls


def test_safe_directory_builds_missing_parent_after_ancestry_check(tmp_path, monkeypatch):
    target = tmp_path / "new-parent" / "new-child"
    assert not target.exists()
    monkeypatch.setattr(
        Path,
        "exists",
        lambda _path: pytest.fail("safe directory creation must use lstat ancestry"),
    )
    assert secret_scan.make_safe_directory(target) == target
    assert secret_scan.stat.S_ISDIR(secret_scan.os.lstat(target).st_mode)


def test_ads_destination_is_rejected(tmp_path):
    source = tmp_path / "private-output"
    source.mkdir()
    (source / "report.txt").write_text("fixture-only report")
    destination = tmp_path / "evidence"
    with pytest.raises(secret_scan.UnsafeScanPath, match="SCAN_DESTINATION_UNSAFE"):
        secret_scan.write_bytes_exclusive(destination / "report.txt:stream", b"x")
    assert not destination.exists()


def test_non_c_path_is_rejected_before_lstat(monkeypatch):
    monkeypatch.setattr(
        secret_scan.os,
        "lstat",
        lambda _path: pytest.fail("non-C path must be rejected before metadata access"),
    )
    with pytest.raises(secret_scan.UnsafeScanPath, match="MUST_BE_ON_C"):
        secret_scan.validate_c_ancestry(Path("D:/unvisited"))


def test_existing_destination_symlink_is_never_followed(tmp_path):
    source = tmp_path / "private-output"
    source.mkdir()
    (source / "report.txt").write_text("clean fixture output")
    outside = tmp_path / "outside.txt"
    outside.write_text("external fixture sentinel")
    destination = tmp_path / "evidence"
    destination.mkdir()
    link = destination / "report.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable in this environment")
    with pytest.raises(secret_scan.UnsafeScanPath, match="ALREADY_EXISTS"):
        module.publish_outputs(source, destination)
    assert outside.read_text() == "external fixture sentinel"


def test_child_output_is_not_echoed_to_terminal(capsys, tmp_path):
    raw = "pytest fixture-only CSRF sentinel"
    module.emit_child_summary(1, raw, raw, tmp_path / "evidence")
    assert raw not in capsys.readouterr().out


def test_caller_selector_is_validated_and_never_serialized(capsys):
    sentinel = "tests/test_export_jobs.py::test_case --token=fixture-only-secret"
    with pytest.raises(SystemExit, match="INVALID_TEST_SELECTION"):
        module.validate_selection([sentinel])
    metadata = module.public_selection_metadata([sentinel])
    assert sentinel not in __import__("json").dumps(metadata)
    assert sentinel not in capsys.readouterr().out
    assert "selection_count" in metadata and "selection_sha256" in metadata

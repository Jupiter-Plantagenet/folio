"""Shared, bounded document-intake limits for the API and parser container."""

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES_PER_PACKET = 10
MAX_PAGES_PER_FILE = 50
MAX_PAGES_PER_PACKET = 200
MAX_XLSX_DECOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_XLSX_CELLS = 100_000
MAX_XLSX_ROWS = 1_048_576
MAX_XLSX_COLUMNS = 16_384
MAX_XLSX_CELL_TEXT_BYTES = 64 * 1024
MAX_XLSX_PROJECTED_TEXT_BYTES = 16 * 1024 * 1024
MAX_PARSER_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_PARSER_ERROR_BYTES = 4096
XLSX_OUTPUT_FIXED_BYTES_PER_CELL = 512
XLSX_OUTPUT_VALUE_MULTIPLIER = 30
XLSX_OUTPUT_METADATA_MULTIPLIER = 12

# Legacy JSON clients retain their existing request ceiling. Multipart uploads
# are capped to the maximum permitted raw packet plus small bounded metadata.
MAX_LEGACY_JSON_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_REQUEST_BYTES = MAX_FILE_BYTES * MAX_FILES_PER_PACKET + 1024 * 1024
MAX_MULTIPART_FIELD_BYTES = 64 * 1024


def source_within_packet_limits(file_count, page_count, source_pages):
    """Return whether one nonduplicate source fits the packet's file/page quotas."""
    return (
        0 <= file_count < MAX_FILES_PER_PACKET
        and 1 <= source_pages <= MAX_PAGES_PER_FILE
        and 0 <= page_count <= MAX_PAGES_PER_PACKET
        and page_count + source_pages <= MAX_PAGES_PER_PACKET
    )


def xlsx_cell_count_within_limit(cell_count):
    return 0 <= cell_count <= MAX_XLSX_CELLS


def xlsx_decompressed_bytes_within_limit(decompressed_bytes):
    return 0 <= decompressed_bytes <= MAX_XLSX_DECOMPRESSED_BYTES


def xlsx_projection_within_budget(cell_count, value_bytes, metadata_bytes):
    if (
        cell_count < 0
        or value_bytes < 0
        or metadata_bytes < 0
        or value_bytes > MAX_XLSX_PROJECTED_TEXT_BYTES
    ):
        return False
    estimated = (
        cell_count * XLSX_OUTPUT_FIXED_BYTES_PER_CELL
        + value_bytes * XLSX_OUTPUT_VALUE_MULTIPLIER
        + metadata_bytes * XLSX_OUTPUT_METADATA_MULTIPLIER
        + 4096
    )
    return estimated <= MAX_PARSER_OUTPUT_BYTES - MAX_PARSER_ERROR_BYTES

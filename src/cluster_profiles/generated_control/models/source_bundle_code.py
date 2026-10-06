from typing import Literal

SourceBundleCode = Literal['bundle.archive_too_large', 'bundle.digest_invalid', 'bundle.digest_mismatch', 'bundle.duplicate_path', 'bundle.empty', 'bundle.entry_forbidden', 'bundle.expanded_too_large', 'bundle.file_invalid', 'bundle.file_too_large', 'bundle.invalid_archive', 'bundle.manifest_invalid', 'bundle.metadata_mismatch', 'bundle.not_found', 'bundle.path_forbidden', 'bundle.path_too_long', 'bundle.read_failed', 'bundle.size_mismatch', 'bundle.storage_collision', 'bundle.storage_conflict', 'bundle.storage_unavailable', 'bundle.too_many_files', 'source.digest_mismatch']

SOURCE_BUNDLE_CODE_VALUES: set[SourceBundleCode] = { 'bundle.archive_too_large', 'bundle.digest_invalid', 'bundle.digest_mismatch', 'bundle.duplicate_path', 'bundle.empty', 'bundle.entry_forbidden', 'bundle.expanded_too_large', 'bundle.file_invalid', 'bundle.file_too_large', 'bundle.invalid_archive', 'bundle.manifest_invalid', 'bundle.metadata_mismatch', 'bundle.not_found', 'bundle.path_forbidden', 'bundle.path_too_long', 'bundle.read_failed', 'bundle.size_mismatch', 'bundle.storage_collision', 'bundle.storage_conflict', 'bundle.storage_unavailable', 'bundle.too_many_files', 'source.digest_mismatch',  }

def check_source_bundle_code(value: str) -> SourceBundleCode:
    if value in SOURCE_BUNDLE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SOURCE_BUNDLE_CODE_VALUES!r}")

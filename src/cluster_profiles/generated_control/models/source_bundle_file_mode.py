from typing import Literal

SourceBundleFileMode = Literal[420, 493]

SOURCE_BUNDLE_FILE_MODE_VALUES: set[SourceBundleFileMode] = { 420, 493,  }

def check_source_bundle_file_mode(value: int) -> SourceBundleFileMode:
    if value in SOURCE_BUNDLE_FILE_MODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SOURCE_BUNDLE_FILE_MODE_VALUES!r}")

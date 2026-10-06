from typing import Literal

PrebuiltImageCode = Literal['prebuilt.build_key_mismatch', 'prebuilt.not_pinned', 'prebuilt.pull_failed_recently', 'prebuilt.used']

PREBUILT_IMAGE_CODE_VALUES: set[PrebuiltImageCode] = { 'prebuilt.build_key_mismatch', 'prebuilt.not_pinned', 'prebuilt.pull_failed_recently', 'prebuilt.used',  }

def check_prebuilt_image_code(value: str) -> PrebuiltImageCode:
    if value in PREBUILT_IMAGE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PREBUILT_IMAGE_CODE_VALUES!r}")

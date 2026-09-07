from typing import Literal, cast

RuntimeImageReceiptSource = Literal['controller-build', 'published']

RUNTIME_IMAGE_RECEIPT_SOURCE_VALUES: set[RuntimeImageReceiptSource] = { 'controller-build', 'published',  }

def check_runtime_image_receipt_source(value: str) -> RuntimeImageReceiptSource:
    if value in RUNTIME_IMAGE_RECEIPT_SOURCE_VALUES:
        return cast(RuntimeImageReceiptSource, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_IMAGE_RECEIPT_SOURCE_VALUES!r}")

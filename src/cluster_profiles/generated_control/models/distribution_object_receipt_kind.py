from typing import Literal, cast

DistributionObjectReceiptKind = Literal['model', 'oci-archive']

DISTRIBUTION_OBJECT_RECEIPT_KIND_VALUES: set[DistributionObjectReceiptKind] = { 'model', 'oci-archive',  }

def check_distribution_object_receipt_kind(value: str) -> DistributionObjectReceiptKind:
    if value in DISTRIBUTION_OBJECT_RECEIPT_KIND_VALUES:
        return cast(DistributionObjectReceiptKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {DISTRIBUTION_OBJECT_RECEIPT_KIND_VALUES!r}")

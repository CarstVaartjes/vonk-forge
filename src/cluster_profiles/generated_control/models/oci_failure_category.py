from typing import Literal

OciFailureCategory = Literal['artifact', 'capacity', 'image-digest', 'metadata', 'process', 'reconciliation-busy', 'runtime', 'storage', 'storage-not-found', 'storage-permission-denied', 'workload']

OCI_FAILURE_CATEGORY_VALUES: set[OciFailureCategory] = { 'artifact', 'capacity', 'image-digest', 'metadata', 'process', 'reconciliation-busy', 'runtime', 'storage', 'storage-not-found', 'storage-permission-denied', 'workload',  }

def check_oci_failure_category(value: str) -> OciFailureCategory:
    if value in OCI_FAILURE_CATEGORY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {OCI_FAILURE_CATEGORY_VALUES!r}")

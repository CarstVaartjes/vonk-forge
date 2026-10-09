from typing import Literal

CertificateIssuancePurpose = Literal['enrollment', 'rotation']

CERTIFICATE_ISSUANCE_PURPOSE_VALUES: set[CertificateIssuancePurpose] = { 'enrollment', 'rotation',  }

def check_certificate_issuance_purpose(value: str) -> CertificateIssuancePurpose:
    if value in CERTIFICATE_ISSUANCE_PURPOSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_ISSUANCE_PURPOSE_VALUES!r}")

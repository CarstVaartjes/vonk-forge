from typing import Literal

CertificateIssuanceBindingPurpose = Literal['enrollment', 'rotation']

CERTIFICATE_ISSUANCE_BINDING_PURPOSE_VALUES: set[CertificateIssuanceBindingPurpose] = { 'enrollment', 'rotation',  }

def check_certificate_issuance_binding_purpose(value: str) -> CertificateIssuanceBindingPurpose:
    if value in CERTIFICATE_ISSUANCE_BINDING_PURPOSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_ISSUANCE_BINDING_PURPOSE_VALUES!r}")

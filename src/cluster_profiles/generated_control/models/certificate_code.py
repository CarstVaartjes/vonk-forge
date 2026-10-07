from typing import Literal

CertificateCode = Literal['certificate.response_unrepresentable']

CERTIFICATE_CODE_VALUES: set[CertificateCode] = { 'certificate.response_unrepresentable',  }

def check_certificate_code(value: str) -> CertificateCode:
    if value in CERTIFICATE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_CODE_VALUES!r}")

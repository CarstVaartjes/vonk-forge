from typing import Literal

CertificateRequestMode = Literal['issue', 'observe']

CERTIFICATE_REQUEST_MODE_VALUES: set[CertificateRequestMode] = { 'issue', 'observe',  }

def check_certificate_request_mode(value: str) -> CertificateRequestMode:
    if value in CERTIFICATE_REQUEST_MODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_REQUEST_MODE_VALUES!r}")

from typing import Literal

CertificateState = Literal['expired', 'inactive', 'missing', 'not-yet-valid', 'revoked', 'valid']

CERTIFICATE_STATE_VALUES: set[CertificateState] = { 'expired', 'inactive', 'missing', 'not-yet-valid', 'revoked', 'valid',  }

def check_certificate_state(value: str) -> CertificateState:
    if value in CERTIFICATE_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_STATE_VALUES!r}")

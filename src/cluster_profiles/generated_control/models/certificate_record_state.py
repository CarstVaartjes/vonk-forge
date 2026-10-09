from typing import Literal

CertificateRecordState = Literal['active', 'revoked', 'staged']

CERTIFICATE_RECORD_STATE_VALUES: set[CertificateRecordState] = { 'active', 'revoked', 'staged',  }

def check_certificate_record_state(value: str) -> CertificateRecordState:
    if value in CERTIFICATE_RECORD_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_RECORD_STATE_VALUES!r}")

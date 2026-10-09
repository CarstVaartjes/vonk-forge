from typing import Literal

CertificateRotationState = Literal['issuing', 'manual-recovery', 'revocation-pending', 'revoked']

CERTIFICATE_ROTATION_STATE_VALUES: set[CertificateRotationState] = { 'issuing', 'manual-recovery', 'revocation-pending', 'revoked',  }

def check_certificate_rotation_state(value: str) -> CertificateRotationState:
    if value in CERTIFICATE_ROTATION_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_ROTATION_STATE_VALUES!r}")

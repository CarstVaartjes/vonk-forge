from typing import Literal

CertificateCode = Literal['certificate.attempt_superseded', 'certificate.authentication_refused', 'certificate.binding_refused', 'certificate.issuance_in_progress', 'certificate.issuance_revoked', 'certificate.issuance_unavailable', 'certificate.request_binding_mismatch', 'certificate.request_invalid', 'certificate.response_unrepresentable', 'certificate.rotation_source_revoked', 'certificate.serial_already_issued', 'certificate.serial_already_reserved', 'certificate.source_identity_refused', 'certificate.source_revoked']

CERTIFICATE_CODE_VALUES: set[CertificateCode] = { 'certificate.attempt_superseded', 'certificate.authentication_refused', 'certificate.binding_refused', 'certificate.issuance_in_progress', 'certificate.issuance_revoked', 'certificate.issuance_unavailable', 'certificate.request_binding_mismatch', 'certificate.request_invalid', 'certificate.response_unrepresentable', 'certificate.rotation_source_revoked', 'certificate.serial_already_issued', 'certificate.serial_already_reserved', 'certificate.source_identity_refused', 'certificate.source_revoked',  }

def check_certificate_code(value: str) -> CertificateCode:
    if value in CERTIFICATE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_CODE_VALUES!r}")

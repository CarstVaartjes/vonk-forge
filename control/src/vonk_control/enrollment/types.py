"""Durable token-authorized enrollment for immutable GPU node identities."""

from __future__ import annotations

import re

from vonk_agent_protocol import (
    CertificateCode,
    InvalidRequestError,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)

_NODE_ID = re.compile(r"spk_[0-9a-f]{32}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_ENROLLMENT_GRANT_TTL_SECONDS = 900


class EnrollmentDenied(SecurityRefusalError, RuntimeError):
    """Enrollment input or state does not authorize the requested operation."""

    def __init__(
        self,
        message: str,
        *,
        reason: SecurityRefusalReason = SecurityRefusalReason.CONTROLLER_FLEET_ENROLLMENT_DENIED,
    ) -> None:
        super().__init__(message, reason=reason)


class ExpiredRenewalGraceExhausted(EnrollmentDenied):
    """The enrolled key no longer authorizes unattended certificate recovery."""

    reason_code = SecurityRefusalReason.AGENT_EXPIRED_RENEWAL_GRACE_EXHAUSTED

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=self.reason_code)


class EnrollmentIssuanceUncertain(UnknownOutcomeError, RuntimeError):
    """Provider evidence is unknown; reconcile the durable exact request on retry."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=WaitReason.OBSERVATION_UNAVAILABLE)


class CertificateResponseCapacityRefused(InvalidRequestError):
    """The provider refused representability before committing any certificate."""

    reason_code = CertificateCode.RESPONSE_UNREPRESENTABLE


class RemoteRevocationUncertain(UnknownOutcomeError, RuntimeError):
    """Local denial committed, but provider confirmation remains pending."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=WaitReason.OBSERVATION_UNAVAILABLE)


class RenewalInProgress(UnknownOutcomeError, RuntimeError):
    """A committed renewal owner may still persist its provider result."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=WaitReason.OBSERVATION_UNAVAILABLE)


class RenewalIssuanceUncertain(UnknownOutcomeError, RuntimeError):
    """Exact renewal evidence is temporarily unavailable or historically unknown."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=WaitReason.OBSERVATION_UNAVAILABLE)


class RenewalConflictRevocationUncertain(UnknownOutcomeError, RuntimeError):
    """An obsolete staged certificate is denied locally but CA revocation is pending."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=WaitReason.OBSERVATION_UNAVAILABLE)


from ..enrollment_contract import (
    EnrollmentGrant as EnrollmentGrant,  # noqa: PLC0414 -- shared export
)
from ..enrollment_contract import EnrollmentIssuanceClaim as _IssuanceClaim
from ..enrollment_contract import EnrollmentRotationClaim as _RotationClaim
from ..enrollment_contract import (
    EnrollmentRotationRecoveryClaim as _RotationRecoveryClaim,
)

__all__ = [
    "EnrollmentGrant",
    "_IssuanceClaim",
    "_RotationClaim",
    "_RotationRecoveryClaim",
]

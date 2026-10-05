from typing import Literal

InstallPartialEvidenceReason = Literal['external-member', 'installation-not-installed', 'mapping-incomplete', 'missing-ranks', 'rank-incomplete-bytes', 'rank-membership-mismatch', 'rank-not-installed', 'unexpected-ranks']

INSTALL_PARTIAL_EVIDENCE_REASON_VALUES: set[InstallPartialEvidenceReason] = { 'external-member', 'installation-not-installed', 'mapping-incomplete', 'missing-ranks', 'rank-incomplete-bytes', 'rank-membership-mismatch', 'rank-not-installed', 'unexpected-ranks',  }

def check_install_partial_evidence_reason(value: str) -> InstallPartialEvidenceReason:
    if value in INSTALL_PARTIAL_EVIDENCE_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALL_PARTIAL_EVIDENCE_REASON_VALUES!r}")

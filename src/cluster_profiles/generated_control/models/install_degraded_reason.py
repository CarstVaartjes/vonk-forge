from typing import Literal

InstallDegradedReason = Literal['external-member', 'installation-not-installed', 'mapping-incomplete', 'missing-ranks', 'rank-incomplete-bytes', 'rank-membership-mismatch', 'rank-not-installed', 'unexpected-ranks']

INSTALL_DEGRADED_REASON_VALUES: set[InstallDegradedReason] = { 'external-member', 'installation-not-installed', 'mapping-incomplete', 'missing-ranks', 'rank-incomplete-bytes', 'rank-membership-mismatch', 'rank-not-installed', 'unexpected-ranks',  }

def check_install_degraded_reason(value: str) -> InstallDegradedReason:
    if value in INSTALL_DEGRADED_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALL_DEGRADED_REASON_VALUES!r}")

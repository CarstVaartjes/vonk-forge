from typing import Literal

RunDegradedReason = Literal['external-member', 'mapping-incomplete', 'missing-ranks', 'rank-membership-mismatch', 'rank-not-running', 'rank-stale', 'route-not-published', 'run-not-running', 'unexpected-ranks']

RUN_DEGRADED_REASON_VALUES: set[RunDegradedReason] = { 'external-member', 'mapping-incomplete', 'missing-ranks', 'rank-membership-mismatch', 'rank-not-running', 'rank-stale', 'route-not-published', 'run-not-running', 'unexpected-ranks',  }

def check_run_degraded_reason(value: str) -> RunDegradedReason:
    if value in RUN_DEGRADED_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_DEGRADED_REASON_VALUES!r}")

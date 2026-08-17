from typing import Literal, cast

RunPresenceDegradedReasonType0 = Literal['external-member', 'mapping-incomplete', 'missing-ranks', 'rank-membership-mismatch', 'rank-not-running', 'rank-stale', 'route-not-published', 'run-not-running', 'unexpected-ranks']

RUN_PRESENCE_DEGRADED_REASON_TYPE_0_VALUES: set[RunPresenceDegradedReasonType0] = { 'external-member', 'mapping-incomplete', 'missing-ranks', 'rank-membership-mismatch', 'rank-not-running', 'rank-stale', 'route-not-published', 'run-not-running', 'unexpected-ranks',  }

def check_run_presence_degraded_reason_type_0(value: str) -> RunPresenceDegradedReasonType0:
    if value in RUN_PRESENCE_DEGRADED_REASON_TYPE_0_VALUES:
        return cast(RunPresenceDegradedReasonType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_PRESENCE_DEGRADED_REASON_TYPE_0_VALUES!r}")

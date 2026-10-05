from typing import Literal

WaitReason = Literal['agent-restart-interrupted', 'agent-upgrade-awaiting-identity', 'cleanup-unconfirmed', 'job-state-uncertain', 'job-stop-unconfirmed', 'lease-lapsed', 'legacy-unclassified', 'model-custody-unconfirmed', 'observation-unavailable', 'operation-not-enabled', 'receipt-missing', 'report-uncertain', 'retained-identity-mismatch', 'runtime-effect-unconfirmed', 'scope-changed', 'stale-plan', 'stop-metadata-unconfirmed', 'stop-unconfirmed']

WAIT_REASON_VALUES: set[WaitReason] = { 'agent-restart-interrupted', 'agent-upgrade-awaiting-identity', 'cleanup-unconfirmed', 'job-state-uncertain', 'job-stop-unconfirmed', 'lease-lapsed', 'legacy-unclassified', 'model-custody-unconfirmed', 'observation-unavailable', 'operation-not-enabled', 'receipt-missing', 'report-uncertain', 'retained-identity-mismatch', 'runtime-effect-unconfirmed', 'scope-changed', 'stale-plan', 'stop-metadata-unconfirmed', 'stop-unconfirmed',  }

def check_wait_reason(value: str) -> WaitReason:
    if value in WAIT_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {WAIT_REASON_VALUES!r}")

from typing import Literal

TransientReason = Literal['admission_busy', 'ca_unavailable', 'controller_starting', 'dependency_unavailable', 'local_state_unavailable', 'peer_response_unavailable', 'rate_limited']

TRANSIENT_REASON_VALUES: set[TransientReason] = { 'admission_busy', 'ca_unavailable', 'controller_starting', 'dependency_unavailable', 'local_state_unavailable', 'peer_response_unavailable', 'rate_limited',  }

def check_transient_reason(value: str) -> TransientReason:
    if value in TRANSIENT_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TRANSIENT_REASON_VALUES!r}")

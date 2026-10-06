from typing import Literal

NodeOfflineReason = Literal['agent-inactive', 'agent-revoked', 'certificate-expired', 'certificate-inactive', 'certificate-missing', 'certificate-not-yet-valid', 'certificate-revoked', 'last-seen-in-future', 'never-seen', 'stale', 'unregistered']

NODE_OFFLINE_REASON_VALUES: set[NodeOfflineReason] = { 'agent-inactive', 'agent-revoked', 'certificate-expired', 'certificate-inactive', 'certificate-missing', 'certificate-not-yet-valid', 'certificate-revoked', 'last-seen-in-future', 'never-seen', 'stale', 'unregistered',  }

def check_node_offline_reason(value: str) -> NodeOfflineReason:
    if value in NODE_OFFLINE_REASON_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NODE_OFFLINE_REASON_VALUES!r}")

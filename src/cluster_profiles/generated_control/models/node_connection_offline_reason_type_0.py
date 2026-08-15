from typing import Literal, cast

NodeConnectionOfflineReasonType0 = Literal['agent-inactive', 'agent-revoked', 'certificate-expired', 'certificate-inactive', 'certificate-missing', 'certificate-not-yet-valid', 'certificate-revoked', 'last-seen-in-future', 'never-seen', 'stale', 'unregistered']

NODE_CONNECTION_OFFLINE_REASON_TYPE_0_VALUES: set[NodeConnectionOfflineReasonType0] = { 'agent-inactive', 'agent-revoked', 'certificate-expired', 'certificate-inactive', 'certificate-missing', 'certificate-not-yet-valid', 'certificate-revoked', 'last-seen-in-future', 'never-seen', 'stale', 'unregistered',  }

def check_node_connection_offline_reason_type_0(value: str) -> NodeConnectionOfflineReasonType0:
    if value in NODE_CONNECTION_OFFLINE_REASON_TYPE_0_VALUES:
        return cast(NodeConnectionOfflineReasonType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NODE_CONNECTION_OFFLINE_REASON_TYPE_0_VALUES!r}")

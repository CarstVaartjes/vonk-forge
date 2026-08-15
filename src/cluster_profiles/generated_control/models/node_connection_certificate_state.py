from typing import Literal, cast

NodeConnectionCertificateState = Literal['expired', 'inactive', 'missing', 'not-yet-valid', 'revoked', 'valid']

NODE_CONNECTION_CERTIFICATE_STATE_VALUES: set[NodeConnectionCertificateState] = { 'expired', 'inactive', 'missing', 'not-yet-valid', 'revoked', 'valid',  }

def check_node_connection_certificate_state(value: str) -> NodeConnectionCertificateState:
    if value in NODE_CONNECTION_CERTIFICATE_STATE_VALUES:
        return cast(NodeConnectionCertificateState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {NODE_CONNECTION_CERTIFICATE_STATE_VALUES!r}")

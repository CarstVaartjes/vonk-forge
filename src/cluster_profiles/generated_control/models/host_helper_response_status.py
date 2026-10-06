from typing import Literal

HostHelperResponseStatus = Literal['container-runtime-request-executed', 'container-runtime-stop-uncertain', 'package-activation-confirmed', 'package-installed', 'rejected']

HOST_HELPER_RESPONSE_STATUS_VALUES: set[HostHelperResponseStatus] = { 'container-runtime-request-executed', 'container-runtime-stop-uncertain', 'package-activation-confirmed', 'package-installed', 'rejected',  }

def check_host_helper_response_status(value: str) -> HostHelperResponseStatus:
    if value in HOST_HELPER_RESPONSE_STATUS_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {HOST_HELPER_RESPONSE_STATUS_VALUES!r}")

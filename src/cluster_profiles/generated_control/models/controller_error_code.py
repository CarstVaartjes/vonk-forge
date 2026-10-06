from typing import Literal

ControllerErrorCode = Literal['controller.conflict', 'controller.fleet.revocation_uncertain', 'controller.fleet.upgrade_conflict', 'controller.http_', 'controller.internal_error', 'controller.invalid_request', 'controller.not_found', 'controller.rate_limited', 'controller.request_too_large', 'controller.timeout', 'controller.unavailable']

CONTROLLER_ERROR_CODE_VALUES: set[ControllerErrorCode] = { 'controller.conflict', 'controller.fleet.revocation_uncertain', 'controller.fleet.upgrade_conflict', 'controller.http_', 'controller.internal_error', 'controller.invalid_request', 'controller.not_found', 'controller.rate_limited', 'controller.request_too_large', 'controller.timeout', 'controller.unavailable',  }

def check_controller_error_code(value: str) -> ControllerErrorCode:
    if value in CONTROLLER_ERROR_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CONTROLLER_ERROR_CODE_VALUES!r}")

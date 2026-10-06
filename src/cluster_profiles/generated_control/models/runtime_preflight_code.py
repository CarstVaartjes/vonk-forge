from typing import Literal

RuntimePreflightCode = Literal['runtime_preflight.capability_failed', 'runtime_preflight.child_missing', 'runtime_preflight.execution_failed', 'runtime_preflight.host_changed', 'runtime_preflight.node_missing', 'runtime_preflight.node_revoked', 'runtime_preflight.operation_missing', 'runtime_preflight.receipt_invalid', 'runtime_preflight.receipt_missing', 'runtime_preflight.required', 'runtime_preflight.requirement_unknown', 'runtime_preflight.requirements_changed', 'runtime_preflight.stale']

RUNTIME_PREFLIGHT_CODE_VALUES: set[RuntimePreflightCode] = { 'runtime_preflight.capability_failed', 'runtime_preflight.child_missing', 'runtime_preflight.execution_failed', 'runtime_preflight.host_changed', 'runtime_preflight.node_missing', 'runtime_preflight.node_revoked', 'runtime_preflight.operation_missing', 'runtime_preflight.receipt_invalid', 'runtime_preflight.receipt_missing', 'runtime_preflight.required', 'runtime_preflight.requirement_unknown', 'runtime_preflight.requirements_changed', 'runtime_preflight.stale',  }

def check_runtime_preflight_code(value: str) -> RuntimePreflightCode:
    if value in RUNTIME_PREFLIGHT_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUNTIME_PREFLIGHT_CODE_VALUES!r}")

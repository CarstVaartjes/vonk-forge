from typing import Literal, cast

RunSwitchReasonScope = Literal['artifact', 'conflict', 'freshness', 'group', 'mapping', 'model', 'node', 'operation', 'recipe']

RUN_SWITCH_REASON_SCOPE_VALUES: set[RunSwitchReasonScope] = { 'artifact', 'conflict', 'freshness', 'group', 'mapping', 'model', 'node', 'operation', 'recipe',  }

def check_run_switch_reason_scope(value: str) -> RunSwitchReasonScope:
    if value in RUN_SWITCH_REASON_SCOPE_VALUES:
        return cast(RunSwitchReasonScope, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_REASON_SCOPE_VALUES!r}")

from typing import Literal

RunSwitchJobPayloadOperationKind = Literal['recipe.cleanup.v2', 'recipe.run-switch.v2', 'recipe.stop.v2']

RUN_SWITCH_JOB_PAYLOAD_OPERATION_KIND_VALUES: set[RunSwitchJobPayloadOperationKind] = { 'recipe.cleanup.v2', 'recipe.run-switch.v2', 'recipe.stop.v2',  }

def check_run_switch_job_payload_operation_kind(value: str) -> RunSwitchJobPayloadOperationKind:
    if value in RUN_SWITCH_JOB_PAYLOAD_OPERATION_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_JOB_PAYLOAD_OPERATION_KIND_VALUES!r}")

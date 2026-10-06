from typing import Literal

RunSwitchJobPayloadAction = Literal['cleanup', 'install', 'run', 'stop', 'switch']

RUN_SWITCH_JOB_PAYLOAD_ACTION_VALUES: set[RunSwitchJobPayloadAction] = { 'cleanup', 'install', 'run', 'stop', 'switch',  }

def check_run_switch_job_payload_action(value: str) -> RunSwitchJobPayloadAction:
    if value in RUN_SWITCH_JOB_PAYLOAD_ACTION_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_JOB_PAYLOAD_ACTION_VALUES!r}")

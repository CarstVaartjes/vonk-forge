from typing import Literal, cast

RunSwitchMemberProgressState = Literal['failed', 'pending', 'running', 'succeeded', 'unknown']

RUN_SWITCH_MEMBER_PROGRESS_STATE_VALUES: set[RunSwitchMemberProgressState] = { 'failed', 'pending', 'running', 'succeeded', 'unknown',  }

def check_run_switch_member_progress_state(value: str) -> RunSwitchMemberProgressState:
    if value in RUN_SWITCH_MEMBER_PROGRESS_STATE_VALUES:
        return cast(RunSwitchMemberProgressState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_MEMBER_PROGRESS_STATE_VALUES!r}")

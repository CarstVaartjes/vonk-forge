from typing import Literal, cast

RunSwitchContainerBuildResultState = Literal['building', 'failed', 'planned', 'succeeded']

RUN_SWITCH_CONTAINER_BUILD_RESULT_STATE_VALUES: set[RunSwitchContainerBuildResultState] = { 'building', 'failed', 'planned', 'succeeded',  }

def check_run_switch_container_build_result_state(value: str) -> RunSwitchContainerBuildResultState:
    if value in RUN_SWITCH_CONTAINER_BUILD_RESULT_STATE_VALUES:
        return cast(RunSwitchContainerBuildResultState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CONTAINER_BUILD_RESULT_STATE_VALUES!r}")

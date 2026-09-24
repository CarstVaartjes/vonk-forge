from typing import Literal, cast

RunSwitchInstallationVerifyResultSubphaseType0 = Literal['container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy']

RUN_SWITCH_INSTALLATION_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES: set[RunSwitchInstallationVerifyResultSubphaseType0] = { 'container-build', 'model-download', 'runtime-image', 'runtime-install', 'runtime-plan', 'target-copy',  }

def check_run_switch_installation_verify_result_subphase_type_0(value: str) -> RunSwitchInstallationVerifyResultSubphaseType0:
    if value in RUN_SWITCH_INSTALLATION_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES:
        return cast(RunSwitchInstallationVerifyResultSubphaseType0, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_INSTALLATION_VERIFY_RESULT_SUBPHASE_TYPE_0_VALUES!r}")

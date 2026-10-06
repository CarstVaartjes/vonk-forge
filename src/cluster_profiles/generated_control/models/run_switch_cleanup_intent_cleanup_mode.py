from typing import Literal

RunSwitchCleanupIntentCleanupMode = Literal['reconcile', 'uninstall']

RUN_SWITCH_CLEANUP_INTENT_CLEANUP_MODE_VALUES: set[RunSwitchCleanupIntentCleanupMode] = { 'reconcile', 'uninstall',  }

def check_run_switch_cleanup_intent_cleanup_mode(value: str) -> RunSwitchCleanupIntentCleanupMode:
    if value in RUN_SWITCH_CLEANUP_INTENT_CLEANUP_MODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CLEANUP_INTENT_CLEANUP_MODE_VALUES!r}")

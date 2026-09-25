from typing import Literal, cast

RunSwitchCleanupPreviewRequestCleanupMode = Literal['reconcile', 'uninstall']

RUN_SWITCH_CLEANUP_PREVIEW_REQUEST_CLEANUP_MODE_VALUES: set[RunSwitchCleanupPreviewRequestCleanupMode] = { 'reconcile', 'uninstall',  }

def check_run_switch_cleanup_preview_request_cleanup_mode(value: str) -> RunSwitchCleanupPreviewRequestCleanupMode:
    if value in RUN_SWITCH_CLEANUP_PREVIEW_REQUEST_CLEANUP_MODE_VALUES:
        return cast(RunSwitchCleanupPreviewRequestCleanupMode, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RUN_SWITCH_CLEANUP_PREVIEW_REQUEST_CLEANUP_MODE_VALUES!r}")

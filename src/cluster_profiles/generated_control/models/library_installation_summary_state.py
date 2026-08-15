from typing import Literal, cast

LibraryInstallationSummaryState = Literal['failed', 'installed', 'installing', 'partial', 'planned']

LIBRARY_INSTALLATION_SUMMARY_STATE_VALUES: set[LibraryInstallationSummaryState] = { 'failed', 'installed', 'installing', 'partial', 'planned',  }

def check_library_installation_summary_state(value: str) -> LibraryInstallationSummaryState:
    if value in LIBRARY_INSTALLATION_SUMMARY_STATE_VALUES:
        return cast(LibraryInstallationSummaryState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {LIBRARY_INSTALLATION_SUMMARY_STATE_VALUES!r}")

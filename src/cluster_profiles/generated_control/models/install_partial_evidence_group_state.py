from typing import Literal

InstallPartialEvidenceGroupState = Literal['failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled']

INSTALL_PARTIAL_EVIDENCE_GROUP_STATE_VALUES: set[InstallPartialEvidenceGroupState] = { 'failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled',  }

def check_install_partial_evidence_group_state(value: str) -> InstallPartialEvidenceGroupState:
    if value in INSTALL_PARTIAL_EVIDENCE_GROUP_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALL_PARTIAL_EVIDENCE_GROUP_STATE_VALUES!r}")

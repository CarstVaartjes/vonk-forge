from typing import Literal

InstallPartialEvidenceRankState = Literal['failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled']

INSTALL_PARTIAL_EVIDENCE_RANK_STATE_VALUES: set[InstallPartialEvidenceRankState] = { 'failed', 'installed', 'installing', 'partial', 'planned', 'uninstalled',  }

def check_install_partial_evidence_rank_state(value: str) -> InstallPartialEvidenceRankState:
    if value in INSTALL_PARTIAL_EVIDENCE_RANK_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INSTALL_PARTIAL_EVIDENCE_RANK_STATE_VALUES!r}")

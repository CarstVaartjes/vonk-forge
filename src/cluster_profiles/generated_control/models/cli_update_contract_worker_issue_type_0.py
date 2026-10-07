from typing import Literal

CliUpdateContractWorkerIssueType0 = Literal['api-worker-contract-unavailable', 'worker-contract-incompatible', 'worker-contract-mixed', 'worker-observation-unavailable', 'worker-provenance-unavailable', 'worker-source-mixed']

CLI_UPDATE_CONTRACT_WORKER_ISSUE_TYPE_0_VALUES: set[CliUpdateContractWorkerIssueType0] = { 'api-worker-contract-unavailable', 'worker-contract-incompatible', 'worker-contract-mixed', 'worker-observation-unavailable', 'worker-provenance-unavailable', 'worker-source-mixed',  }

def check_cli_update_contract_worker_issue_type_0(value: str) -> CliUpdateContractWorkerIssueType0:
    if value in CLI_UPDATE_CONTRACT_WORKER_ISSUE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CLI_UPDATE_CONTRACT_WORKER_ISSUE_TYPE_0_VALUES!r}")

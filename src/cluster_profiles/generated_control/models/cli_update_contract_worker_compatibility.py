from typing import Literal

CliUpdateContractWorkerCompatibility = Literal['compatible', 'incompatible', 'unknown']

CLI_UPDATE_CONTRACT_WORKER_COMPATIBILITY_VALUES: set[CliUpdateContractWorkerCompatibility] = { 'compatible', 'incompatible', 'unknown',  }

def check_cli_update_contract_worker_compatibility(value: str) -> CliUpdateContractWorkerCompatibility:
    if value in CLI_UPDATE_CONTRACT_WORKER_COMPATIBILITY_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CLI_UPDATE_CONTRACT_WORKER_COMPATIBILITY_VALUES!r}")

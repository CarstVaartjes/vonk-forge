from typing import Literal, cast

WorkloadProvenanceRankAgreement = Literal['match', 'mismatch', 'unknown']

WORKLOAD_PROVENANCE_RANK_AGREEMENT_VALUES: set[WorkloadProvenanceRankAgreement] = { 'match', 'mismatch', 'unknown',  }

def check_workload_provenance_rank_agreement(value: str) -> WorkloadProvenanceRankAgreement:
    if value in WORKLOAD_PROVENANCE_RANK_AGREEMENT_VALUES:
        return cast(WorkloadProvenanceRankAgreement, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {WORKLOAD_PROVENANCE_RANK_AGREEMENT_VALUES!r}")

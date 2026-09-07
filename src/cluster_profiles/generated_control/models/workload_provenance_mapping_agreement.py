from typing import Literal, cast

WorkloadProvenanceMappingAgreement = Literal['match', 'mismatch', 'unknown']

WORKLOAD_PROVENANCE_MAPPING_AGREEMENT_VALUES: set[WorkloadProvenanceMappingAgreement] = { 'match', 'mismatch', 'unknown',  }

def check_workload_provenance_mapping_agreement(value: str) -> WorkloadProvenanceMappingAgreement:
    if value in WORKLOAD_PROVENANCE_MAPPING_AGREEMENT_VALUES:
        return cast(WorkloadProvenanceMappingAgreement, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {WORKLOAD_PROVENANCE_MAPPING_AGREEMENT_VALUES!r}")

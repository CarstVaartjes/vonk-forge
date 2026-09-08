from typing import Literal, cast

RankProvenanceIdentityAgreement = Literal['match', 'mismatch', 'unknown']

RANK_PROVENANCE_IDENTITY_AGREEMENT_VALUES: set[RankProvenanceIdentityAgreement] = { 'match', 'mismatch', 'unknown',  }

def check_rank_provenance_identity_agreement(value: str) -> RankProvenanceIdentityAgreement:
    if value in RANK_PROVENANCE_IDENTITY_AGREEMENT_VALUES:
        return cast(RankProvenanceIdentityAgreement, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RANK_PROVENANCE_IDENTITY_AGREEMENT_VALUES!r}")

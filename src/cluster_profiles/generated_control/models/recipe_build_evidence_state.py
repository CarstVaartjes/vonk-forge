from typing import Literal, cast

RecipeBuildEvidenceState = Literal['available', 'building', 'failed', 'incompatible', 'missing', 'planned', 'unknown']

RECIPE_BUILD_EVIDENCE_STATE_VALUES: set[RecipeBuildEvidenceState] = { 'available', 'building', 'failed', 'incompatible', 'missing', 'planned', 'unknown',  }

def check_recipe_build_evidence_state(value: str) -> RecipeBuildEvidenceState:
    if value in RECIPE_BUILD_EVIDENCE_STATE_VALUES:
        return cast(RecipeBuildEvidenceState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_BUILD_EVIDENCE_STATE_VALUES!r}")

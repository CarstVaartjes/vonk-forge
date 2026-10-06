from typing import Literal

RecipeStartPayloadPhaseType0 = Literal['collective-readiness', 'rank-launch']

RECIPE_START_PAYLOAD_PHASE_TYPE_0_VALUES: set[RecipeStartPayloadPhaseType0] = { 'collective-readiness', 'rank-launch',  }

def check_recipe_start_payload_phase_type_0(value: str) -> RecipeStartPayloadPhaseType0:
    if value in RECIPE_START_PAYLOAD_PHASE_TYPE_0_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_START_PAYLOAD_PHASE_TYPE_0_VALUES!r}")

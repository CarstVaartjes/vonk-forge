from typing import Literal, cast

PlacementRecommendationInstallState = Literal['complete', 'not_present', 'partial', 'unknown']

PLACEMENT_RECOMMENDATION_INSTALL_STATE_VALUES: set[PlacementRecommendationInstallState] = { 'complete', 'not_present', 'partial', 'unknown',  }

def check_placement_recommendation_install_state(value: str) -> PlacementRecommendationInstallState:
    if value in PLACEMENT_RECOMMENDATION_INSTALL_STATE_VALUES:
        return cast(PlacementRecommendationInstallState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_RECOMMENDATION_INSTALL_STATE_VALUES!r}")

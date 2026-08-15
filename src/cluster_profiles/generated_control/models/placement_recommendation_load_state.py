from typing import Literal, cast

PlacementRecommendationLoadState = Literal['loaded', 'not_loaded', 'unknown']

PLACEMENT_RECOMMENDATION_LOAD_STATE_VALUES: set[PlacementRecommendationLoadState] = { 'loaded', 'not_loaded', 'unknown',  }

def check_placement_recommendation_load_state(value: str) -> PlacementRecommendationLoadState:
    if value in PLACEMENT_RECOMMENDATION_LOAD_STATE_VALUES:
        return cast(PlacementRecommendationLoadState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_RECOMMENDATION_LOAD_STATE_VALUES!r}")

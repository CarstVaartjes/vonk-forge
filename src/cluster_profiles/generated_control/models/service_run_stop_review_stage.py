from typing import Literal

ServiceRunStopReviewStage = Literal['accepted', 'dispatched', 'withdrawal-claimed']

SERVICE_RUN_STOP_REVIEW_STAGE_VALUES: set[ServiceRunStopReviewStage] = { 'accepted', 'dispatched', 'withdrawal-claimed',  }

def check_service_run_stop_review_stage(value: str) -> ServiceRunStopReviewStage:
    if value in SERVICE_RUN_STOP_REVIEW_STAGE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SERVICE_RUN_STOP_REVIEW_STAGE_VALUES!r}")

from typing import Literal, cast

Spark3542CompatibilityRecoveryAbandonPreviewResponseState = Literal['abandoned', 'preview']

SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_PREVIEW_RESPONSE_STATE_VALUES: set[Spark3542CompatibilityRecoveryAbandonPreviewResponseState] = { 'abandoned', 'preview',  }

def check_spark_3542_compatibility_recovery_abandon_preview_response_state(value: str) -> Spark3542CompatibilityRecoveryAbandonPreviewResponseState:
    if value in SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_PREVIEW_RESPONSE_STATE_VALUES:
        return cast(Spark3542CompatibilityRecoveryAbandonPreviewResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_PREVIEW_RESPONSE_STATE_VALUES!r}")

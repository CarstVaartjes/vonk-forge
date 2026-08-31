from typing import Literal, cast

Spark3542CompatibilityRecoveryAbandonResponseState = Literal['abandoned', 'preview']

SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_RESPONSE_STATE_VALUES: set[Spark3542CompatibilityRecoveryAbandonResponseState] = { 'abandoned', 'preview',  }

def check_spark_3542_compatibility_recovery_abandon_response_state(value: str) -> Spark3542CompatibilityRecoveryAbandonResponseState:
    if value in SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_RESPONSE_STATE_VALUES:
        return cast(Spark3542CompatibilityRecoveryAbandonResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_RESPONSE_STATE_VALUES!r}")

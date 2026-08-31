from typing import Literal, cast

Spark3542CompatibilityRecoveryResponseState = Literal['armed', 'awaiting-identity', 'completed', 'completed-before-dispatch', 'issued', 'operator-blocked', 'preview']

SPARK_3542_COMPATIBILITY_RECOVERY_RESPONSE_STATE_VALUES: set[Spark3542CompatibilityRecoveryResponseState] = { 'armed', 'awaiting-identity', 'completed', 'completed-before-dispatch', 'issued', 'operator-blocked', 'preview',  }

def check_spark_3542_compatibility_recovery_response_state(value: str) -> Spark3542CompatibilityRecoveryResponseState:
    if value in SPARK_3542_COMPATIBILITY_RECOVERY_RESPONSE_STATE_VALUES:
        return cast(Spark3542CompatibilityRecoveryResponseState, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_3542_COMPATIBILITY_RECOVERY_RESPONSE_STATE_VALUES!r}")

from typing import Literal, cast

Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition = Literal['issued-and-expired', 'never-issued']

SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_RESPONSE_GRANT_DISPOSITION_VALUES: set[Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition] = { 'issued-and-expired', 'never-issued',  }

def check_spark_3542_compatibility_recovery_abandon_response_grant_disposition(value: str) -> Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition:
    if value in SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_RESPONSE_GRANT_DISPOSITION_VALUES:
        return cast(Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_RESPONSE_GRANT_DISPOSITION_VALUES!r}")

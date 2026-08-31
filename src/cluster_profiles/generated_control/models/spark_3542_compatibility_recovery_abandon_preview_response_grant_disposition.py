from typing import Literal, cast

Spark3542CompatibilityRecoveryAbandonPreviewResponseGrantDisposition = Literal['issued-and-expired', 'never-issued']

SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_PREVIEW_RESPONSE_GRANT_DISPOSITION_VALUES: set[Spark3542CompatibilityRecoveryAbandonPreviewResponseGrantDisposition] = { 'issued-and-expired', 'never-issued',  }

def check_spark_3542_compatibility_recovery_abandon_preview_response_grant_disposition(value: str) -> Spark3542CompatibilityRecoveryAbandonPreviewResponseGrantDisposition:
    if value in SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_PREVIEW_RESPONSE_GRANT_DISPOSITION_VALUES:
        return cast(Spark3542CompatibilityRecoveryAbandonPreviewResponseGrantDisposition, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {SPARK_3542_COMPATIBILITY_RECOVERY_ABANDON_PREVIEW_RESPONSE_GRANT_DISPOSITION_VALUES!r}")

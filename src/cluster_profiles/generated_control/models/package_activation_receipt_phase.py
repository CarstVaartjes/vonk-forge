from typing import Literal, cast

PackageActivationReceiptPhase = Literal['acknowledged', 'activation_failed', 'armed', 'rollback_failed', 'rolled_back', 'rolling_back']

PACKAGE_ACTIVATION_RECEIPT_PHASE_VALUES: set[PackageActivationReceiptPhase] = { 'acknowledged', 'activation_failed', 'armed', 'rollback_failed', 'rolled_back', 'rolling_back',  }

def check_package_activation_receipt_phase(value: str) -> PackageActivationReceiptPhase:
    if value in PACKAGE_ACTIVATION_RECEIPT_PHASE_VALUES:
        return cast(PackageActivationReceiptPhase, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PACKAGE_ACTIVATION_RECEIPT_PHASE_VALUES!r}")

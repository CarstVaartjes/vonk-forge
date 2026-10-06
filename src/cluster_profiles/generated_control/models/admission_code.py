from typing import Literal

AdmissionCode = Literal['admission.capacity_busy']

ADMISSION_CODE_VALUES: set[AdmissionCode] = { 'admission.capacity_busy',  }

def check_admission_code(value: str) -> AdmissionCode:
    if value in ADMISSION_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ADMISSION_CODE_VALUES!r}")

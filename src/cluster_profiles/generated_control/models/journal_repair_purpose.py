from typing import Literal

JournalRepairPurpose = Literal['cancellation', 'measurement', 'owner_observation']

JOURNAL_REPAIR_PURPOSE_VALUES: set[JournalRepairPurpose] = { 'cancellation', 'measurement', 'owner_observation',  }

def check_journal_repair_purpose(value: str) -> JournalRepairPurpose:
    if value in JOURNAL_REPAIR_PURPOSE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {JOURNAL_REPAIR_PURPOSE_VALUES!r}")

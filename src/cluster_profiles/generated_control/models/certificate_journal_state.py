from typing import Literal

CertificateJournalState = Literal['absent', 'issued', 'pending']

CERTIFICATE_JOURNAL_STATE_VALUES: set[CertificateJournalState] = { 'absent', 'issued', 'pending',  }

def check_certificate_journal_state(value: str) -> CertificateJournalState:
    if value in CERTIFICATE_JOURNAL_STATE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {CERTIFICATE_JOURNAL_STATE_VALUES!r}")

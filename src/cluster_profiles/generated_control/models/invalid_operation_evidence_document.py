from typing import Literal, cast

InvalidOperationEvidenceDocument = Literal['payload', 'result']

INVALID_OPERATION_EVIDENCE_DOCUMENT_VALUES: set[InvalidOperationEvidenceDocument] = { 'payload', 'result',  }

def check_invalid_operation_evidence_document(value: str) -> InvalidOperationEvidenceDocument:
    if value in INVALID_OPERATION_EVIDENCE_DOCUMENT_VALUES:
        return cast(InvalidOperationEvidenceDocument, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {INVALID_OPERATION_EVIDENCE_DOCUMENT_VALUES!r}")

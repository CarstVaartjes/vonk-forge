"""Failures of worker coordinators that have not claimed an operation."""

from typing import Annotated

from pydantic import Field

from vonk_agent_protocol.agent_words import Disposition
from vonk_agent_protocol.reason_codes.vocabulary import OperationReason
from vonk_control.lifecycle.evidence import Residue
from vonk_control.strict_json import StrictModel
from vonk_control.wait_contract import QueueWait


class CoordinatorFailureEvidence(StrictModel):
    source_content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    disposition: Disposition
    reason: OperationReason
    detail: Annotated[str, Field(min_length=1, max_length=512)]


class CoordinatorFailureView(StrictModel):
    source: str
    failure: CoordinatorFailureEvidence | Residue
    wait: QueueWait | None

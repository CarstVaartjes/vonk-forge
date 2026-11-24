"""Stop identity survives full Controller storage."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from vonk_agent_protocol.agent_operations import AgentOperation
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.jobs.documents import RecipeStopParent, controller_recipe_document
from vonk_control.models.jobs import Job
from vonk_control.recipe_operations.service import RecipeOperationService


def _read_authority(job: Job) -> RecipeStopParent:
    return RecipeOperationService._service_stop_document(job)


def _persist_authority(job: Job, document: RecipeStopParent) -> None:
    RecipeOperationService._write_stop_parent(job, document, now=datetime.now(UTC))


def test_stop_writer_and_reader_bind_the_same_typed_content() -> None:
    document = RecipeStopParent(
        schema_version=1,
        owner_kind="run",
        owner_id=str(uuid4()),
        plan_digest="a" * 64,
    )
    job = Job(
        kind=AgentOperation.RECIPE_STOP.value,
        payload=controller_recipe_document(document),
        payload_digest=hashlib.sha256(canonical_message(document)).hexdigest(),
    )
    assert _read_authority(job) == document
    _persist_authority(job, document)
    assert _read_authority(job) == document

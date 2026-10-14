"""A start payload the installed plan cannot build ends the start with its cause.

Production shape: an artifact-job recipe (``settings.kind == "job"``) loaded by
a profile reaches ``start``; its compiled plan has no serving endpoint, so the
start payload fails validation on every attempt.  It used to be wrapped as
``RecipeRetryLater("recipe start payload observation is unavailable")`` with the
cause dropped, and the run-switch re-entered the start phase forever.
"""

import pytest
from sqlalchemy import delete, select
from vonk_agent_protocol import (
    AgentOperation,
    InvalidRequestError,
    UnknownOutcomeError,
)
from vonk_control.failure_classification import error_detail, is_transient
from vonk_control.models import AgentPresence, Job, RecipeRun

from .test_artifact_jobs import _configure_artifact_recipe
from .test_recipe_operations import installed_recipe, setup_services


def test_unbuildable_start_payload_ends_with_node_and_cause(tmp_path):
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=_configure_artifact_recipe
    )
    installed = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="1" * 36
    )
    plan = service.preview_run(installed.owner_id, "image-job")

    with pytest.raises(InvalidRequestError) as raised:
        service.start(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
        )

    error = raised.value
    assert not isinstance(error, UnknownOutcomeError)
    assert not is_transient(error)
    root = error
    while root.__cause__ is not None:
        root = root.__cause__
    # The evidence names the node and carries the validation cause itself.
    assert nodes[0] in str(error)
    assert error_detail(root).split(": ", 1)[1] in str(error)
    with sessions() as session:
        assert list(session.scalars(select(RecipeRun))) == []
        assert (
            session.scalar(
                select(Job).where(Job.kind == AgentOperation.RECIPE_START.value)
            )
            is None
        )


def test_missing_endpoint_presence_waits_and_names_the_node(tmp_path):
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="1" * 36
    )
    plan = service.preview_run(installed.owner_id, "qwen")
    with sessions.begin() as session:
        session.execute(delete(AgentPresence))

    with pytest.raises(UnknownOutcomeError) as raised:
        service.start(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
        )

    assert is_transient(raised.value)
    assert nodes[0] in str(raised.value)

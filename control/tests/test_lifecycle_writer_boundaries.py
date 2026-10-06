"""Prove the lifecycle-writers ratchet flags each wrong write and passes the gate.

Every rule runs against a fixture with the shape that went wrong (a state
assigned outside the core, a bulk update, a constructor) and the closest shape
that must stay quiet (a non-lifecycle table, a comparison, the core itself).
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from .lifecycle_writer_boundaries import (
    ATTRIBUTE,
    BULK_UPDATE,
    CONSTRUCTOR,
    CORE_PREFIX,
    DICT_ITEM,
    DICT_STATE_OWNERS,
    HELPER_CALL,
    NON_LIFECYCLE_STATE_VARIABLES,
    REPO_ROOT,
    Write,
    evaluate_writer_gate,
    scan_lifecycle_writes,
    scan_source,
    scan_unresolved,
)

OWNER = "control/src/vonk_control/agent_jobs.py"
OTHER = "control/src/vonk_control/sample.py"


def _scanned(source: str, path: str = OTHER) -> list[tuple[str, str, str]]:
    return [
        (write.model, write.kind, write.function)
        for write in scan_source(dedent(source), path=path)
    ]


def test_an_attribute_write_on_a_typed_parameter_is_a_write() -> None:
    sites = _scanned(
        """
        from .models import AgentOperation as StoredOperation

        def park(operation: StoredOperation | None) -> None:
            operation.state = "waiting-for-operator"
        """
    )
    assert sites == [("AgentOperation", ATTRIBUTE, "park")]


def test_a_row_read_from_the_session_is_resolved() -> None:
    sites = _scanned(
        """
        from .models import Job, ArtifactJob

        class Service:
            def one(self, session, job_id):
                job = session.get(Job, job_id)
                job.state = "failed"

            def two(self, session):
                for row in session.scalars(select(ArtifactJob).where(x)):
                    row.state = "cancelled"

            def three(self, session):
                statement = select(Job).where(y)
                found = session.scalar(statement)
                found.state = "queued"
        """
    )
    assert sites == [
        ("Job", ATTRIBUTE, "Service.one"),
        ("ArtifactJob", ATTRIBUTE, "Service.two"),
        ("Job", ATTRIBUTE, "Service.three"),
    ]


def test_a_tuple_return_annotation_types_the_unpacked_rows() -> None:
    sites = _scanned(
        """
        from .models import AgentOperation, AgentOperationAttempt

        class Service:
            def _active(self) -> tuple[AgentOperation, AgentOperationAttempt]: ...

            def finish(self):
                operation, attempt = self._active()
                attempt.state = "failed"
                operation.state = "failed"
        """
    )
    assert sorted(sites) == [
        ("AgentOperation", ATTRIBUTE, "Service.finish"),
        ("AgentOperationAttempt", ATTRIBUTE, "Service.finish"),
    ]


def test_bulk_update_constructor_setattr_and_helper_are_writes() -> None:
    sites = _scanned(
        """
        from .models import Job, ModelCacheOperation

        def bulk(session):
            session.execute(update(Job).where(Job.id == 1).values(state="queued"))

        def build(session):
            session.add(ModelCacheOperation(id="x", state="queued"))

        def reflect(job: Job):
            setattr(job, "state", "failed")

        class Service:
            def go(self, session, row):
                self._set_application_state(session, row, "failed")
        """
    )
    assert sites == [
        ("Job", BULK_UPDATE, "bulk"),
        ("ModelCacheOperation", CONSTRUCTOR, "build"),
        ("Job", ATTRIBUTE, "reflect"),
        ("FleetProfileApplication", HELPER_CALL, "Service.go"),
    ]


def test_a_state_dict_store_is_a_write_only_in_an_owner_module() -> None:
    source = 'def go(application):\n    application["state"] = "failed"\n'
    assert [kind for _, kind, _ in _scanned(source, path=OWNER)] == [DICT_ITEM]
    assert _scanned(source, path=OTHER) == []


@pytest.mark.parametrize(
    "source",
    [
        (
            "from .models import RecipeInstallation\n"
            "def f(installation: RecipeInstallation):\n    installation.state = 'x'\n"
        ),
        "from .models import Job\ndef f(job: Job):\n    return job.state == 'queued'\n",
        "from .models import Job\ndef f(job: Job):\n    job.status_reason = 'queued'\n",
        (
            "from vonk_agent_protocol import AgentOperation\n"
            "def f(operation: AgentOperation):\n    operation.state = 'x'\n"
        ),
    ],
)
def test_other_tables_comparisons_and_protocol_enums_are_not_writes(
    source: str,
) -> None:
    assert _scanned(source) == []


def test_the_core_and_the_models_module_are_never_scanned() -> None:
    source = "from .models import Job\ndef f(job: Job):\n    job.state = 'x'\n"
    assert scan_source(source, path=CORE_PREFIX + "core.py") == []
    assert scan_source(source, path="control/src/vonk_control/models.py") == []


def _write(function: str = "f", model: str = "Job", kind: str = ATTRIBUTE) -> Write:
    return Write(OTHER, function, model, kind, 1)


def test_the_gate_allows_no_write_outside_the_core() -> None:
    assert evaluate_writer_gate([]) == []

    found = evaluate_writer_gate([_write(), _write("g")])
    assert len(found) == 2
    assert all("outside vonk_control.lifecycle" in message for message in found)


def test_the_repository_writes_no_lifecycle_state_outside_the_core() -> None:
    assert evaluate_writer_gate(scan_lifecycle_writes()) == []


def test_no_allowlist_file_is_left_to_grow_back() -> None:
    assert not (REPO_ROOT / "tools" / "lifecycle-writers-allowlist.json").exists()


def test_no_unresolved_state_write_hides_in_a_lifecycle_module() -> None:
    """A ``.state`` write the resolver could not type must be a known non-lifecycle row."""

    unexplained: list[str] = []
    from .lifecycle_writer_boundaries import CONTROL_SOURCE_ROOT, REPO_ROOT

    for module in sorted(CONTROL_SOURCE_ROOT.rglob("*.py")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        if relative not in DICT_STATE_OWNERS:
            continue
        known = NON_LIFECYCLE_STATE_VARIABLES.get(relative, frozenset())
        for function, variable, line in scan_unresolved(
            module.read_text(encoding="utf-8"), path=relative
        ):
            if variable not in known:
                unexplained.append(f"{relative}:{line}: {variable}.state in {function}")
    assert unexplained == []

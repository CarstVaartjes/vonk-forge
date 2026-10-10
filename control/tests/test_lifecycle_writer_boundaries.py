"""Prove the lifecycle-writers ownership rule flags each wrong write and passes the gate.

Every rule runs against a fixture with the shape that went wrong (a state
assigned outside the core, a bulk update, a constructor) and the closest shape
that must stay quiet (a non-lifecycle table, a comparison, the core itself).
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from .lifecycle_writer_boundaries import (
    ATTRIBUTE,
    CORE_PREFIX,
    DICT_STATE_OWNERS,
    NON_LIFECYCLE_STATE_VARIABLES,
    REPO_ROOT,
    Write,
    evaluate_writer_gate,
    scan_lifecycle_writes,
    scan_source,
    scan_unresolved,
)

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")

OWNER = "control/src/vonk_control/agent_jobs/completion.py"
OTHER = "control/src/vonk_control/sample.py"


def _scanned(source: str, path: str = OTHER) -> list[str]:
    return evaluate_writer_gate(scan_source(dedent(source), path=path))


def test_an_attribute_write_on_a_typed_parameter_is_a_write() -> None:
    sites = _scanned(
        """
        from .models import AgentOperation as StoredOperation

        def park(operation: StoredOperation | None) -> None:
            operation.state = "waiting-for-operator"
        """
    )
    assert sites


@pytest.mark.parametrize(
    "body",
    [
        "job = session.get(Job, key)\njob.state = 'failed'",
        "for row in session.scalars(select(ArtifactJob).where(x)):\n    row.state = 'cancelled'",
        "statement = select(Job).where(y)\nfound = session.scalar(statement)\nfound.state = 'queued'",
    ],
)
def test_a_row_read_from_the_session_is_resolved(body: str) -> None:
    from textwrap import indent

    source = "from .models import Job, ArtifactJob\ndef update(session):\n" + indent(
        body, "    "
    )
    assert _scanned(source)


@pytest.mark.parametrize("target", ["operation", "attempt"])
def test_a_tuple_return_annotation_types_the_unpacked_rows(target: str) -> None:
    source = f"""
    from .models import AgentOperation, AgentOperationAttempt
    class Service:
        def _active(self) -> tuple[AgentOperation, AgentOperationAttempt]: ...
        def finish(self):
            operation, attempt = self._active()
            {target}.state = "failed"
    """
    assert _scanned(source)


@pytest.mark.parametrize(
    "body",
    [
        "session.execute(update(Job).where(Job.id == 1).values(state='queued'))",
        "session.add(ModelCacheOperation(id='x', state='queued'))",
        "setattr(job, 'state', 'failed')",
        "self._set_application_state(session, job, 'failed')",
    ],
)
def test_bulk_update_constructor_setattr_and_helper_are_writes(body: str) -> None:
    source = f"from .models import Job, ModelCacheOperation\ndef update(self, session, job: Job):\n    {body}"
    assert _scanned(source)


def test_a_state_dict_store_is_a_write_only_in_an_owner_module() -> None:
    source = 'def go(application):\n    application["state"] = "failed"\n'
    assert _scanned(source, path=OWNER)
    assert not _scanned(source, path=OTHER)


def test_a_job_argument_does_not_turn_a_typed_other_row_into_a_job() -> None:
    source = """
        from .models import Job, RecipeRun

        def run_for(job: Job) -> RecipeRun | None:
            return None

        def job_for(run: RecipeRun) -> Job | None:
            return None

        def update(session, job: Job):
            run = run_for(job)
            run.state = "running"
            direct = session.get(RecipeRun, job.id)
            direct.state = "running"
            recovered = job_for(run)
            recovered.state = "failed"
            unknown = undocumented(job)
            unknown.state = "failed"
    """
    assert _scanned(source)


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
    assert not _scanned(source)


def test_the_core_and_the_models_module_are_never_scanned() -> None:
    source = "from .models import Job\ndef f(job: Job):\n    job.state = 'x'\n"
    assert not scan_source(source, path=CORE_PREFIX + "core.py")
    assert not scan_source(source, path="control/src/vonk_control/models.py")


def _write(function: str = "f", model: str = "Job", kind: str = ATTRIBUTE) -> Write:
    return Write(OTHER, function, model, kind, 1)


def test_the_gate_allows_no_write_outside_the_core() -> None:
    assert evaluate_writer_gate([]) == []

    found = evaluate_writer_gate([_write(), _write("g")])
    assert found


def test_the_repository_writes_no_lifecycle_state_outside_the_core() -> None:
    assert evaluate_writer_gate(scan_lifecycle_writes()) == []


def test_no_unresolved_state_write_hides_in_a_lifecycle_module() -> None:
    """A ``.state`` write the resolver could not type must be a known non-lifecycle row."""

    unexplained: list[str] = []
    from .lifecycle_writer_boundaries import CONTROL_SOURCE_ROOT
    from .parsed_sources import parsed_tree

    for module, parsed in parsed_tree(CONTROL_SOURCE_ROOT):
        relative = module.relative_to(REPO_ROOT).as_posix()
        if relative not in DICT_STATE_OWNERS:
            continue
        known = NON_LIFECYCLE_STATE_VARIABLES.get(relative, frozenset())
        for function, variable, line in scan_unresolved(
            parsed.source, path=relative, tree=parsed.tree
        ):
            if variable not in known:
                unexplained.append(f"{relative}:{line}: {variable}.state in {function}")
    assert unexplained == []


@pytest.mark.parametrize("level", [".", "..", "..."])
def test_package_relative_model_imports_keep_lifecycle_and_workload_rows_distinct(
    level,
):
    source = f"""
    from {level}models import Job, RecipeBuild
    def project(session):
        job = session.get(Job, "job")
        build = session.get(RecipeBuild, "build")
        job.state = "failed"
        build.state = "failed"
    """
    assert _scanned(source)

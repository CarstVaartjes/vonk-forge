"""Contract literal detection distinguishes authored consumers from generated words."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    SecurityRefusalReason,
    WaitReason,
)

from . import vocabulary_literals as scan

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")

REL = "control/src/vonk_control/example.py"


def test_rust_path_components_are_not_contract_states() -> None:
    # Wrong implementation: a cache directory called distribution was treated
    # as a hand-spelled Controller capability and blocked valid preparation.
    source = 'let cache = root.join("distribution");\nlet state = "running";\n'
    assert scan.scan_source(
        source,
        path="rust/crates/vonk-agent/src/executor/example.rs",
        words=frozenset({"distribution", "running"}),
    ) == [2]


def _module(tmp_path: Path, source: str, relative: str = REL) -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _python(
    tmp_path: Path, source: str, relative: str = REL
) -> Counter[tuple[str, str]]:
    return scan.scan_python([_module(tmp_path, source, relative)], root=tmp_path)


def test_a_hand_spelled_word_is_found_in_code_not_in_a_docstring(
    tmp_path: Path,
) -> None:
    counts = _python(
        tmp_path,
        '''
"""A module that mentions waiting-for-operator in prose."""

def wait() -> str:
    """Parks as "waiting-for-operator"."""
    state = "waiting-for-operator"
    code = "operation_cancelled"
    return state if code else "running"
''',
    )

    assert counts


def test_prose_words_and_unrelated_strings_are_not_literals(tmp_path: Path) -> None:
    counts = _python(
        tmp_path,
        'WORDS = ("unknown", "none", "stop", "retry", "waiting", "needs operator")\n',
    )

    assert not counts


def test_generated_python_is_not_scanned(tmp_path: Path) -> None:
    relative = "src/cluster_profiles/generated_control/models/state.py"

    assert not _python(tmp_path, 'X = "needs-operator"\n', relative)


def test_typescript_sources_may_not_spell_the_words(tmp_path: Path) -> None:
    source = _module(
        tmp_path,
        "if (state === \"waiting-for-operator\" || kind === 'needs-operator') {}\n",
        "control/web/src/pages/example.tsx",
    )
    generated = _module(
        tmp_path,
        'export const X = "waiting-for-operator";\n',
        "control/web/src/api/vocabulary.generated.ts",
    )
    fixture = _module(
        tmp_path,
        'const state = "waiting-for-operator";\n',
        "control/web/src/pages/example.test.tsx",
    )

    counts = scan.scan_typescript([source, generated, fixture], root=tmp_path)

    assert counts


# Parses and walks every Python module of the repository three times over.


def test_the_web_app_spells_no_vocabulary_word_by_hand() -> None:
    assert scan.scan_typescript() == Counter()


@pytest.mark.parametrize(
    "source",
    [
        'if row.state == "expired":\n    pass\n',
        'ACTIVE_STATES = ("queued", "waiting")\n',
        'x = Job(state="cancelling")\n',
        'ok = attempt.state in {"expired", State.FAILED}\n',
        'm = {"partial": State.BACKOFF}\n',
    ],
)
def test_a_retired_state_spelling_is_found_where_a_statement_names_a_state(
    tmp_path: Path, source: str
) -> None:
    assert _python(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        'activity = "waiting"\n',
        'phase = "partial"\n',
        'label = "cancelling"  # a progress word, not a stored state\n',
        'def f(state):\n    """expired"""\n    return 1\n',
        "def f(row):\n    row.state = 1\n    return 'waiting'\n",
    ],
)
def test_the_same_words_elsewhere_are_prose(tmp_path: Path, source: str) -> None:
    assert ("legacy_state", REL) not in _python(tmp_path, source)


def test_only_the_alias_table_may_spell_the_retired_words(tmp_path: Path) -> None:
    contract = "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py"
    assert not _python(tmp_path, 'STORED_STATE = "waiting"\n', contract)
    assert _python(tmp_path, 'STORED_STATE = "waiting"\n')


@pytest.mark.parametrize(
    "source",
    [
        'if installation.state == "installed":\n    pass\n',
        'RECIPE_STATES = ("uninstalled", "failed")\n',
        'x = Run(route_state="withdrawn")\n',
        'ok = assignment.state in {"published", "stopped"}\n',
    ],
)
def test_a_machine_state_spelling_is_found_where_a_statement_names_a_state(
    tmp_path: Path, source: str
) -> None:
    assert _python(tmp_path, source)


@pytest.mark.parametrize(
    "source",
    [
        'label = "installed"\n',
        'result = {"stopped": True, "state": 1}\n',
        'def f(state):\n    """installed"""\n    return 1\n',
    ],
)
def test_the_same_words_elsewhere_are_prose_or_flags(
    tmp_path: Path, source: str
) -> None:
    assert ("machine_state", REL) not in _python(tmp_path, source)


def test_the_contract_module_of_the_machines_may_spell_them(tmp_path: Path) -> None:
    contract = "agent_protocol/src/vonk_agent_protocol/state_machines.py"
    assert not _python(tmp_path, 'STATE = "uninstalled"\n', contract)
    assert _python(tmp_path, 'state = "uninstalled"\n')


def test_the_enum_members_are_not_literals(tmp_path: Path) -> None:
    counts = _python(
        tmp_path,
        "from vonk_agent_protocol import RunAdmissionCode\n"
        "code = RunAdmissionCode.PORT_OCCUPIED\n",
    )

    assert not counts


def test_a_hand_spelled_reason_code_is_found_even_as_a_message_prefix(
    tmp_path: Path,
) -> None:
    counts = _python(
        tmp_path,
        """
CODE = "reconcile.membership_changed"
MESSAGE = "run-switch.plan_blocked: the plan changed"
F_MESSAGE = f"run-switch.plan_blocked: {CODE}"
PROSE = "stop everything"
OTHER = "recipe-installation"
""",
    )

    assert counts


def test_a_free_string_code_position_is_found(tmp_path: Path) -> None:
    source = """
class WidgetError(Exception):
    code = "widget.invalid"

    def __init__(self, code: str, detail: str) -> None:
        self.code = code


class BrokenWidget(WidgetError):
    pass


def _reason(code: str, detail: str):
    return detail


def use(error, blockers):
    raise BrokenWidget("anything.new", "detail")
    raise WidgetError(code="another.new", detail="d")
    blockers.append(make_blocker("a.new.blocker", "d"))
    _reason("fresh.code", "d")
    Reason(reason_code=f"x.{error}")
    return status(status_code=404, exit_code="1")


def defaulted(code: str = "default.code"):
    return code
"""
    path = _module(tmp_path, source)

    found = scan.scan_code_positions([path], root=tmp_path)

    kinds = [line.split(": ", 1)[1].split(":", 1)[0] for line in found]
    assert kinds


def test_a_contract_member_in_a_code_position_is_not_a_literal(
    tmp_path: Path,
) -> None:
    source = """
from vonk_agent_protocol import RunSwitchCode


class WidgetError(Exception):
    code = RunSwitchCode.PLAN_BLOCKED

    def __init__(self, code, detail=""):
        self.code = code


def use():
    raise WidgetError(RunSwitchCode.STALE_PLAN, "d")
    raise WidgetError(code=RunSwitchCode.STALE_PLAN)
"""
    path = _module(tmp_path, source)

    assert scan.scan_code_positions([path], root=tmp_path) == []
    assert not scan.scan_python([path], root=tmp_path)


def test_a_subclass_constructor_without_a_code_takes_a_detail(tmp_path: Path) -> None:
    source = """
from vonk_agent_protocol import RunSwitchCode


class BaseError(Exception):
    def __init__(self, code, detail=""):
        self.code = code


class FixedError(BaseError):
    def __init__(self, detail):
        super().__init__(RunSwitchCode.STALE_PLAN, detail)


def use():
    raise FixedError("the stored image reference is invalid")
"""
    path = _module(tmp_path, source)

    assert scan.scan_code_positions([path], root=tmp_path) == []


# Parses every Controller module twice more.


def test_the_cli_spells_only_contract_codes() -> None:
    import ast
    import re

    from vonk_agent_protocol import REASON_CODE_ENUMS

    from cluster_profiles import cli, cli_render, controller_cli

    members = {
        member.value
        for enum in (*REASON_CODE_ENUMS, SecurityRefusalReason, WaitReason)
        for member in enum
        if any(mark in member.value for mark in scan._SEPARATORS)
    }
    domains = {word.split(".")[0] for word in members if "." in word}
    shaped = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z0-9_.-]+$")
    unknown: set[str] = set()
    for module in (cli, cli_render, controller_cli):
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and shaped.match(node.value)
                and node.value.split(".")[0] in domains
                and not re.search(r"\.v[0-9]+$", node.value)
                and node.value not in members
            ):
                unknown.add(node.value)
    assert not unknown, f"the CLI spells codes the contract does not own: {unknown}"


@pytest.mark.parametrize(
    "source",
    [
        'p = OperationProgress(phase="downloading")\n',
        'p = OperationMemberProgress(phase="a" if x else "transfer", member_id="n")\n',
        'p = {"phase": "completed", "completed_bytes": 3}\n',
        'p = {"phase": "download", "completed_bytes": 3}\n',
        'ok = progress.phase == "completed"\n',
        'ok = prior.measurement.phase in {"queued", "pending"}\n',
        'ok = member.get("phase") != "waiting"\n',
        'm = member.model_copy(update={"phase": "reclaiming"})\n',
    ],
)
def test_a_hand_spelled_progress_phase_is_found_where_progress_is_built_or_read(
    tmp_path: Path, source: str
) -> None:
    # Wrong implementation: the Controller and the agent each spelled the phase,
    # so ``download`` and ``downloading`` named one fact in two ways.
    counts = _python(tmp_path, source)

    assert counts


@pytest.mark.parametrize(
    "source",
    [
        "p = OperationProgress(phase=ProgressPhase.DOWNLOADING)\n",
        'step = {"phase": "transfer", "subphase": "target-copy"}\n',
        'ok = phase.kind == "transfer" and phase.subphase == "model-download"\n',
        'p = OperationProgress(phase=word, kind="prepare")\n',
        'ok = child.phase == "prepare"\n',
        'x = {"phase": "completed"}\n',
    ],
)
def test_a_phase_that_is_not_measured_progress_is_not_a_progress_phase_literal(
    tmp_path: Path, source: str
) -> None:
    assert (scan.PROGRESS_PHASE, REL) not in _python(tmp_path, source)

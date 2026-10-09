"""Each fixture proves a new violation fails while untouched source is ignored."""

import pytest

from .added_line_guards import added_lines, check_source

PATH = "control/src/vonk_control/example.py"


@pytest.mark.parametrize(
    "mode,source,line",
    [
        ("vocabulary", "value = 'running'\n", 1),
        ("mapping", "value: dict[str, object]\n", 1),
        ("identity", "same = a.build_id == b.build_id\n", 1),
        ("security", "raise SecurityRefusalError(reason)\n", 1),
        ("waits", "while True:\n    sleep(1)\n", 1),
        ("waits", "event.wait()\n", 1),
        (
            "reads",
            "@router.get('/x')\ndef read():\n    raise HTTPException(status_code=503)\n",
            3,
        ),
        ("remedies", "message = 'Retry manually'\n", 1),
        (
            "coordination",
            "def work():\n    with session.begin():\n        time.sleep(1)\n",
            3,
        ),
    ],
)
def test_new_occurrence_fails_untouched_occurrence_does_not(mode, source, line):
    assert check_source(
        PATH, source, {line}, modes=(mode,), words=frozenset({"running"})
    )
    assert not check_source(
        PATH,
        source + "\n# unrelated addition\n",
        {len(source.splitlines()) + 2},
        modes=(mode,),
        words=frozenset({"running"}),
    )


@pytest.mark.parametrize(
    "source",
    [
        "while monotonic() < deadline:\n    sleep(1)\n",
        "event.wait(timeout=10)\n",
        "subprocess.run(['x'], timeout=10)\n",
    ],
)
def test_bounded_wait_is_allowed(source):
    assert not check_source(PATH, source, {1, 2}, modes=("waits",))


def test_security_ingress_owner_is_allowed():
    assert not check_source(
        "control/src/vonk_control/security/tokens.py",
        "raise SecurityRefusalError(reason)\n",
        {1},
        modes=("security",),
    )


def test_security_alias_and_local_subclass_cannot_escape():
    source = "from x import SecurityRefusalError as Denied\nclass Refusal(Denied): pass\nraise Refusal(reason)\n"
    assert check_source(PATH, source, {3}, modes=("security",))


def test_patch_context_deleted_lines_and_multiple_hunks():
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n old\n-removed\n+new\n context\n@@ -9 +9,2 @@\n old\n+second\n"
    additions = added_lines(patch)["x.py"]
    source = "# context\nvalue = 'running'\n" + "# context\n" * 7 + "event.wait()\n"
    assert check_source(
        PATH, source, additions, modes=("vocabulary",), words=frozenset({"running"})
    )
    assert check_source(PATH, source, additions, modes=("waits",))
    safe = source.replace("value = 'running'", "value = State.RUNNING").replace(
        "event.wait()", "event.wait(timeout=10)"
    )
    assert not check_source(
        PATH,
        safe,
        additions,
        modes=("vocabulary", "waits"),
        words=frozenset({"running"}),
    )


def test_added_source_that_resembles_a_patch_header():
    patch = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+++ b/inside-source\n"
    assert added_lines(patch) == {"x.py": {1}}


@pytest.mark.parametrize(
    "mode,source",
    [
        ("mapping", "value: dict[str,\n    object]\n"),
        ("identity", "same = (a.build_id ==\n    b.build_id)\n"),
        ("security", "raise SecurityRefused(\n    reason)\n"),
    ],
)
def test_multiline_violation_added_on_a_continuation_line(mode, source):
    assert check_source(PATH, source, {2}, modes=(mode,))


def test_provenance_assignment_identity_is_rejected():
    assert check_source(
        PATH, "same = a.assignment_id == b.assignment_id\n", {1}, modes=("identity",)
    )


def test_contract_definition_can_spell_its_member_but_consumer_cannot():
    path = "agent_protocol/src/vonk_agent_protocol/agent_words.py"
    source = "class Phase(WireEnum):\n    RUNNING = 'running'\n\nvalue = 'running'\n"
    assert not check_source(
        path, source, {2}, modes=("vocabulary",), words=frozenset({"running"})
    )
    assert check_source(
        path, source, {4}, modes=("vocabulary",), words=frozenset({"running"})
    )


def test_new_code_must_be_declared_in_the_contract_first():
    assert check_source(
        PATH, "reason(code='new.code')\n", {1}, modes=("vocabulary",), words=frozenset()
    )


@pytest.mark.parametrize(
    "mode,path,source,line",
    [
        ("waits", "scripts/poll", "#!/bin/bash\nwhile true; do sleep 1; done\n", 2),
        (
            "waits",
            "rust/crates/vonk-agent/src/example.rs",
            "fn poll() { loop { sleep(delay); } }\n",
            1,
        ),
        ("vocabulary", "control/web/src/example.ts", "const state = 'running';\n", 1),
        (
            "tests",
            "control/tests/test_example.py",
            "def test_stale():\n    assert op.state == 'needs-operator'\n",
            2,
        ),
        ("retention", PATH, "class History(Base):\n    __tablename__ = 'history'\n", 1),
    ],
)
def test_other_languages_and_principle_modes_only_check_additions(
    mode, path, source, line
):
    assert check_source(
        path, source, {line}, modes=(mode,), words=frozenset({"running"})
    )
    assert not check_source(
        path,
        source + "\n",
        {len(source.splitlines()) + 1},
        modes=(mode,),
        words=frozenset({"running"}),
    )


def test_web_assertions_are_not_production_contract_literals():
    source = 'expect(state).toBe("running");\n'
    assert not check_source(
        "control/web/src/hooks/observer.test.tsx",
        source,
        {1},
        words=frozenset({"running"}),
    )
    assert check_source(
        "control/web/src/hooks/observer.tsx", source, {1}, words=frozenset({"running"})
    )


def test_rust_test_files_are_not_production_contract_literals():
    assert not check_source(
        "rust/crates/vonk-agent/src/executor/tests.rs",
        'let fixture = "running";\n',
        {1},
        modes=("vocabulary",),
        words=frozenset({"running"}),
    )


def test_rust_inline_test_exemption_does_not_hide_following_production():
    source = """#[cfg(test)]
mod tests {
    fn fixture() { let value = "running"; let brace = "}"; }
}
fn production() { let value = "running"; }
"""
    path = "rust/crates/vonk-agent/src/example.rs"
    assert not check_source(
        path, source, {3}, modes=("vocabulary",), words=frozenset({"running"})
    )
    assert check_source(
        path, source, {5}, modes=("vocabulary",), words=frozenset({"running"})
    )

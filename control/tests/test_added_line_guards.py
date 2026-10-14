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
        # A fixed deadline is the shape P1 forbids for a wait.
        ("waits", "while monotonic() < deadline:\n    sleep(1)\n", 1),
        (
            "waits",
            (
                "def tick(job, now):\n"
                "    if now >= job.created_at + LIMIT:\n"
                "        fail(job)\n"
            ),
            2,
        ),
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
        (
            "retries",
            (
                "try:\n    work()\nexcept (RuntimeError, ValueError) as error:\n"
                "    schedule_retry(job, error)\n"
            ),
            3,
        ),
        (
            "retries",
            (
                "try:\n    work()\nexcept Exception as error:\n"
                "    raise RunSwitchRetryLater(str(error)) from error\n"
            ),
            4,
        ),
        (
            "retries",
            (
                "try:\n    work()\nexcept ValueError as error:\n"
                "    fail(str(error), replan=True)\n"
            ),
            4,
        ),
        # A deterministic error hidden as an unknown outcome is the same wait.
        (
            "retries",
            (
                "try:\n    work()\nexcept (TypeError, ValueError) as error:\n"
                "    raise UnknownOutcomeError(str(error)) from error\n"
            ),
            4,
        ),
        (
            "residues",
            "recipe = Recipe.model_validate(read_row_column(row, 'document'))\n",
            1,
        ),
        (
            "residues",
            "nodes = recipe_topology(\n    read_row_column(row, 'document')\n)\n",
            2,
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
        "while monotonic() < stall_deadline(started, progressed, WINDOW):\n    sleep(1)\n",
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


@pytest.mark.parametrize(
    "path,source,line",
    [
        ("packaging/systemd/new.service", "RestartPreventExitStatus=78\n", 1),
        ("packaging/systemd/new.service", "StartLimitBurst=5\n", 1),
        ("packaging/systemd/new.service", "TimeoutStartSec=0\n", 1),
        ("packaging/systemd/new.service", "Restart=no\n", 1),
        ("packaging/debian/preinst", "exit 78\n", 1),
        ("rust/crates/vonk-agent/src/main.rs", 'state.expect("bookkeeping");\n', 1),
        ("rust/crates/vonk-agent/src/main.rs", "std::process::exit(78);\n", 1),
        ("rust/crates/vonk-agent/src/main.rs", "ExitCode::from(78)\n", 1),
        ("rust/crates/vonk-agent/src/main.rs", 'panic!("local state");\n', 1),
    ],
)
def test_new_permanent_process_stop_fails_without_a_debt_count(path, source, line):
    assert check_source(path, source, {line}, modes=("noexit",))
    if not path.endswith(".service"):
        assert not check_source(path, source, {line + 1}, modes=("noexit",))


def test_daemon_backoff_and_fault_injection_are_allowed():
    assert not check_source(
        "packaging/systemd/new.service",
        "StartLimitIntervalSec=0\nRestart=always\nRestartSec=30s\n",
        {1, 2, 3},
        modes=("noexit",),
    )
    assert not check_source(
        "rust/crates/vonk-agent/src/main.rs",
        "#[cfg(test)]\nmod tests {\n    std::process::exit(78);\n}\n",
        {3},
        modes=("noexit",),
    )


@pytest.mark.parametrize(
    "source",
    [
        "[Service]\nType=simple\nExecStart=/usr/bin/daemon\n",
        "[Service]\nRestart=on-failure\n",
        "[Unit]\nStartLimitIntervalSec=0\n[Service]\nRestart=always\nRestartSec=0\n",
    ],
)
def test_new_service_cannot_omit_recovery_delay_or_disable_recovery(source):
    assert check_source("packaging/systemd/new.service", source, {1}, modes=("noexit",))


def test_relocated_block_carries_debt_once_but_new_copy_and_change_fail():
    removed = "def work():\n    value = 'running'\n    return value\n"
    patch = (
        "diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        + "".join("-" + line + "\n" for line in removed.splitlines())
        + "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,7 @@\n"
        + "".join("+" + line + "\n" for line in removed.splitlines())
        + "+\n"
        + "".join("+" + line + "\n" for line in removed.splitlines())
    )
    assert added_lines(patch)["new.py"] == {4, 5, 6, 7}
    assert check_source(
        PATH,
        removed + "\n" + removed,
        added_lines(patch)["new.py"],
        modes=("vocabulary",),
        words=frozenset({"running"}),
    ) == [f"{PATH}:6: contract literal"]
    changed = patch.replace("+    value = 'running'", "+    value = 'failed'")
    assert added_lines(changed)["new.py"] == set(range(1, 8))


def test_relocated_block_matches_across_hunks_and_spacing_without_crediting_copies():
    """A module split changes diff boundaries; copies and changed text still fail."""
    patch = (
        "diff --git a/old.py b/old.py\n--- a/old.py\n+++ b/old.py\n"
        "@@ -1,2 +0,0 @@\n-def work():\n-    value = 'running'\n"
        "@@ -8 +5,0 @@\n-    return value\n"
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,4 @@\n+def work():\n+\n+    value = 'running'\n+    return value\n"
        "@@ -0,0 +8,3 @@\n+def work():\n+    value = 'running'\n+    return value\n"
    )
    assert added_lines(patch)["new.py"] == {8, 9, 10}
    changed = patch.replace("+    value = 'running'", "+    value = 'failed'")
    assert added_lines(changed)["new.py"] == {1, 2, 3, 4, 8, 9, 10}


@pytest.mark.parametrize(
    ("source", "kept"),
    [
        (
            "Err(_) => return temporary_runtime_observation_failure(),\n",
            "Err(error) => return preparation_failure_at(&error, stage, step),\n",
        ),
        (
            "Err(error) => {\n    let _ = error;\n    return failed(reason);\n}\n",
            "Err(error) => return failed_because(reason, error),\n",
        ),
        # P6: a dropped result, a discarded error arm and an unobserved send.
        (
            "let fingerprint = probe()\n    .ok();\n",
            (
                "let fingerprint = match probe() {\n"
                "    Ok(value) => Some(value),\n"
                '    Err(error) => { eprintln!("{error}"); None }\n};\n'
            ),
        ),
        (
            "match lookup() {\n    Err(_) => None,\n    Ok(v) => Some(v),\n}\n",
            (
                "match lookup() {\n    Err(error) => unknown(error.to_string()),\n"
                "    Ok(v) => Some(v),\n}\n"
            ),
        ),
        (
            (
                "let _ = client.heartbeat_post(&progress).await;\n"
                "let _ = client.report_phase(fence).await;\n"
            ),
            (
                "if let Err(error) = client.post(&progress).await {\n"
                "    record(error);\n}\n"
            ),
        ),
        (
            "    let _ = self.connection.execute(sql, []).ok();\n",
            (
                "    if let Err(error) = self.connection.execute(sql, []) {\n"
                '        eprintln!("{error}");\n    }\n'
            ),
        ),
    ],
)
def test_new_failure_that_discards_its_cause_fails_untouched_does_not(source, kept):
    path = "rust/crates/vonk-agent/src/executor/start.rs"
    lines = set(range(1, len(source.splitlines()) + 1))
    assert check_source(path, source, lines, modes=("causes",))
    assert not check_source(
        path, source, {len(source.splitlines()) + 1}, modes=("causes",)
    )
    assert not check_source(
        path, kept, set(range(1, len(kept.splitlines()) + 1)), modes=("causes",)
    )
    # Test code may drop what it does not assert on.
    assert not check_source(
        "rust/crates/vonk-agent/src/executor/start/tests.rs",
        source,
        lines,
        modes=("causes",),
    )


@pytest.mark.parametrize(
    "source",
    [
        # The handler asks whether the cause is transient.
        (
            "try:\n    work()\nexcept RuntimeError as error:\n"
            "    if is_transient(error):\n        schedule_retry(job, error)\n"
        ),
        (
            "try:\n    work()\nexcept RuntimeError as error:\n"
            "    raise retry_later_unless_deterministic('x', error) from error\n"
        ),
        (
            "try:\n    work()\nexcept ValueError as error:\n"
            "    fail(str(error), definite=not is_transient(error))\n"
        ),
        # A narrow, typed transient family may retry directly.
        (
            "try:\n    work()\nexcept AdmissionLockBusy as busy:\n"
            "    hold_retry(job, busy)\n"
        ),
        # A broad handler that ends or re-raises does not retry.
        "try:\n    work()\nexcept ValueError as error:\n    fail(str(error))\n",
    ],
)
def test_a_retry_that_classifies_its_cause_is_allowed(source):
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(PATH, source, lines, modes=("retries",))


@pytest.mark.parametrize(
    "source",
    [
        'path = case.attrib.get("file")\n',
        'if artifact.get("expired"):\n    pass\n',
        'path = document["file"]\n',
        'document["expired"] = False\n',
        'path = document.pop("file", None)\n',
        'path = document.setdefault("file", None)\n',
    ],
)
def test_structural_field_access_is_not_a_contract_value(source):
    # Wrong implementation: an external XML/JSON field that shares an enum
    # spelling blocks unrelated CI tooling before its checks can execute.
    assert not check_source(
        "scripts/external_protocol_reader.py",
        source,
        set(range(1, len(source.splitlines()) + 1)),
        modes=("vocabulary",),
        words=frozenset({"file", "expired"}),
    )


@pytest.mark.parametrize(
    "source",
    [
        'path = document.get("file", "expired")\n',
        'path = document.pop("file", "expired")\n',
        'document.setdefault("file", "expired")\n',
        'document["file"] = "expired"\n',
        'if document["state"] == "expired":\n    pass\n',
        'handlers = {"expired": handler}\n',
        'handlers = {"security.denied": handler}\n',
        'handler = by_state.get("expired")\n',
        'handler = by_phase["file"]\n',
        'handler = handlers["security.denied"]\n',
        'handler = handlers.get("waiting-for-operator")\n',
    ],
)
def test_field_access_exemption_preserves_values_and_dispatch_keys(source):
    # Wrong implementation: ignoring a whole mapping call or every key hides
    # hand-spelled defaults, assignments, comparisons and enum dispatch tables.
    assert check_source(
        "scripts/external_protocol_reader.py",
        source,
        set(range(1, len(source.splitlines()) + 1)),
        modes=("vocabulary",),
        words=frozenset({"file", "expired", "security.denied", "waiting-for-operator"}),
    )


def test_a_stored_read_checked_for_its_residue_is_allowed():
    source = (
        "document = read_row_column(row, 'document')\n"
        "if isinstance(read_row_column(row, 'labels'), Residue):\n"
        "    pass\n"
    )
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(PATH, source, lines, modes=("residues",))

"""Each fixture proves a new violation fails while untouched source is ignored."""

import pytest
from vonk_agent_protocol import LifecycleState

from .added_line_guards import AGENT_REQUESTS, added_lines, check_source

PATH = "control/src/vonk_control/example.py"


@pytest.mark.parametrize(
    "mode,source,line",
    [
        ("vocabulary", "value = 'running'\n", 1),
        ("mapping", "value: dict[str, object]\n", 1),
        ("identity", "same = a.build_id == b.build_id\n", 1),
        # A recipe's current revision is the catalog head, never a number.
        ("identity", "q = select(R).order_by(R.revision_number.desc())\n", 1),
        ("identity", "best = max(rows, key=lambda row: row.revision_number)\n", 1),
        ("identity", "ok = newest.revision_number <= running.revision_number\n", 1),
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
        # The worker owner's isolation: the same failure retried every tick.
        (
            "repeats",
            (
                "try:\n    result = task()\nexcept Exception:\n"
                "    self._retry_at[task] = now + timedelta(seconds=5)\n"
                "    _LOGGER.exception('observation failed')\n"
            ),
            4,
        ),
        (
            "repeats",
            "try:\n    work()\nexcept OSError:\n    row.next_action_at = due\n",
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
        (
            "claims",
            "rows = select(RecipeBuild).where(RecipeBuild.state.in_(('building',)))\n",
            1,
        ),
        (
            "claims",
            (
                "PENDING = ('planned', 'building')\n"
                "pinned = select(RecipeBuild.plan).where(RecipeBuild.state.in_(PENDING))\n"
            ),
            2,
        ),
        (
            "claims",
            "live = AgentCertificateRotation.state in (CertificateRotationState.ISSUING,)\n",
            1,
        ),
        # A lifecycle reason is a contract enum value, never prose.
        ("reasons", 'job.status_reason = "waiting for the receipt"\n', 1),
        ("reasons", 'finish(job, now, reason=f"failed at {now}", keep=False)\n', 1),
        ("reasons", 'settle(job, CancelRequested(None, "superseded"), now)\n', 1),
        ("reasons", 'why = reason or row.reason or "cancelled"\n', 1),
        ("reasons", 'def _default_reason(row):\n    return "recipe job failed"\n', 2),
        # Movement is forward-only, never "differs from the previous sample".
        (
            "movement",
            (
                "def moved(current, previous):\n"
                "    return current.phase != previous.phase\n"
            ),
            2,
        ),
        (
            "movement",
            (
                "def moved(now, before):\n"
                "    return (now.completed_items or 0) > (before.completed_items or 0)\n"
            ),
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


@pytest.mark.parametrize(
    ("path", "source"),
    [
        # The one module that decides liveness from an owner's stored state.
        (
            "control/src/vonk_control/owner_liveness.py",
            "RecipeBuild.state.in_(('planned', 'building'))\n",
        ),
        # Finished states, and the states of a table that is not a claim.
        (PATH, "RecipeBuild.state.in_(('succeeded', 'failed'))\n"),
        (PATH, "RecipeRun.state.in_(('planned', 'running'))\n"),
        # An instance's state says what the row is, not that its owner works.
        (PATH, "needed = candidate.state in {'planned', 'building'}\n"),
    ],
)
def test_liveness_read_by_owner_liveness_or_off_claims_is_allowed(path, source):
    assert not check_source(path, source, {1}, modes=("claims",))


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


def test_an_edge_answers_403_only_for_a_reviewed_security_refusal():
    edge = "control/src/vonk_control/agent_api/authority.py"
    template = (
        "try:\n"
        "    issue()\n"
        "except {caught}:\n"
        "    raise HTTPException(status_code=403, detail='denied')\n"
    )
    # Stale grant authority caught as a broad error became a 403 (#1579).
    for caught in ("HostHelperAuthorityError", "(EnrollmentDenied, ValueError)"):
        assert check_source(
            edge, template.format(caught=caught), {4}, modes=("security",)
        ) == [f"{edge}:4: 403 for an error that is not a security refusal"]
    assert not check_source(
        edge, template.format(caught="EnrollmentDenied"), {4}, modes=("security",)
    )
    # Untouched lines are not judged.
    assert not check_source(
        edge, template.format(caught="ValueError"), {1}, modes=("security",)
    )


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


def test_a_stored_row_is_never_re_hashed_to_decide_what_it_is():
    rehash = (
        "same = (\n"
        "    job.payload_digest\n"
        "    == hashlib.sha256(canonical_message(job.payload)).hexdigest()\n"
        ")\n"
    )
    assert check_source(PATH, rehash, {3}, modes=("identity",)) == [
        f"{PATH}:3: stored row re-hashed"
    ]
    # Writing the digest, or comparing two recorded digests (a replayed
    # request), is not a re-hash.
    assert not check_source(
        PATH,
        "row.payload_digest = hashlib.sha256(document).hexdigest()\n"
        "same = existing.payload_digest == requested.payload_digest\n",
        {1, 2},
        modes=("identity",),
    )


def test_provenance_assignment_identity_is_rejected():
    assert check_source(
        PATH, "same = a.assignment_id == b.assignment_id\n", {1}, modes=("identity",)
    )


def test_a_primary_key_row_lookup_is_not_a_provenance_comparison():
    assert not check_source(
        PATH,
        "stmt = update(RecipeBuild).where(RecipeBuild.id == build_id)\n",
        {1},
        modes=("identity",),
    )
    assert check_source(
        PATH,
        "same = a.recipe_build_id == b.recipe_build_id\n",
        {1},
        modes=("identity",),
    )
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


def test_a_quote_char_literal_does_not_hide_the_inline_test_module():
    """Catches `'"'` opening a phantom string that swallowed `#[cfg(test)]`.

    rust/crates/vonk-agent/src/failure_evidence.rs trims `'"'` above its
    tests, so every `.expect` added there read as production code.
    """
    source = """fn production() { let _ = text.trim_matches('"'); }
#[cfg(test)]
mod tests {
    fn t() { let lifetime: &'static str = "x"; state.expect("fixture"); }
}
fn later() { state.expect("bookkeeping"); }
"""
    path = "rust/crates/vonk-agent/src/example.rs"
    assert not check_source(path, source, {4}, modes=("noexit",))
    assert check_source(path, source, {6}, modes=("noexit",))


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
    ("source", "kept"),
    [
        (
            # A stop-uncertain reply made up a timeout exit for its job.
            "Err(OperationError::StopUncertain) => (status, Some(124), None),\n",
            "Err(OperationError::StopUncertain) => (status, None, None),\n",
        ),
        (
            "RuntimeStartLaunch::TimedOut => Ok((Some(124), None)),\n",
            "RuntimeStartLaunch::Job { timeout } => self.wait(timeout),\n",
        ),
        (
            "Outcome { exit_code: Some(1), evidence: None }\n",
            "Outcome { exit_code: Some(code), evidence: None }\n",
        ),
        (
            "Ok((Some(0), None))\n",
            "Ok((Some(JOB_ENDED_BY_HELPER_EXIT_CODE), None))\n",
        ),
    ],
)
def test_a_new_invented_exit_code_fails_a_measured_one_does_not(source, kept):
    path = "rust/crates/vonk-agent-helper/src/operations/dispatch.rs"
    assert check_source(path, source, {1}, modes=("causes",))
    assert not check_source(path, kept, {1}, modes=("causes",))
    # Test code may build any exit it asserts on.
    assert not check_source(
        "rust/crates/vonk-agent-helper/src/operations/start/tests.rs",
        source,
        {1},
        modes=("causes",),
    )


@pytest.mark.parametrize(
    "source",
    [
        # The handler asks whether the cause is transient.
        (
            "try:\n    work()\nexcept RuntimeError as error:\n"
            "    if not is_definite(error):\n        schedule_retry(job, error)\n"
        ),
        (
            "try:\n    work()\nexcept RuntimeError as error:\n"
            "    raise retry_later_unless_deterministic('x', error) from error\n"
        ),
        (
            "try:\n    work()\nexcept ValueError as error:\n"
            "    fail(str(error), definite=is_definite(error))\n"
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


def test_a_scheduled_retry_that_shows_its_repetition_is_allowed():
    source = (
        "try:\n    result = task()\nexcept Exception as error:\n"
        "    self._retry_at[task] = now + timedelta(seconds=5)\n"
        "    note_repeat(session, owner, node_id, code(error), now=now)\n"
    )
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(PATH, source, lines, modes=("repeats",))


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


@pytest.mark.parametrize(
    "source",
    [
        "finish(job, now, reason=WaitReason.SCOPE_CHANGED, keep=False)\n",
        "settle(job, CancelRequested(None, SupersedeCode.SUPERSEDED_BY_INTENT), now)\n",
        "why = reason or row.reason or FailureCode.OPERATION_CANCELLED\n",
        "job.status_reason = None\n",
        "job.status_reason = reason[:512]\n",
        'raise InvalidValue("bad input", reason=InvalidRequestReason.MALFORMED)\n',
        'class StopCode(WireEnum):\n    STOP_REASON = "stop.reason"\n',
    ],
)
def test_a_typed_lifecycle_reason_is_allowed(source):
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(PATH, source, lines, modes=("reasons",))


def test_a_controller_test_sends_the_agents_pinned_request_never_its_own():
    # The hand-built JobRun Stop that agreed with the Controller, not the agent.
    source = (
        "stop_plan = stop_payload_from_job_run(plan, cancel_pending_start=False)\n"
        "request = HostRuntimeRequest(\n"
        "    action='stop', fence=fence, arguments=[], stop_plan=stop_plan\n"
        ")\n"
    )
    path = "control/tests/test_example.py"
    assert len(check_source(path, source, {1, 2, 3}, modes=("boundaries",))) == 2
    assert not check_source(path, source + "\nx = 1\n", {6}, modes=("boundaries",))
    assert not check_source(AGENT_REQUESTS, source, {1, 2}, modes=("boundaries",))


def test_an_exclusive_flock_is_taken_only_through_a_held_lock():
    # The pre-fix base-image lock: released only by closing its descriptor, a
    # child forked by another thread kept it held and the next locker was busy.
    raw = (
        "match rustix::fs::flock(&lock, rustix::fs::FlockOperation"
        "::NonBlockingLockExclusive) {\n"
    )
    agent = "rust/crates/vonk-agent/src/base_images.rs"
    assert check_source(agent, raw, {1}, modes=("locks",))
    assert check_source(
        agent, "flock(&f, FlockOperation::LockExclusive)?;\n", {1}, modes=("locks",)
    )
    assert check_source(
        agent, "libc::flock(fd, libc::LOCK_EX);\n", {1}, modes=("locks",)
    )
    held = "let _held = HeldLock::try_exclusive(lock.try_clone()?)?;\n"
    assert not check_source(agent, held, {1}, modes=("locks",))
    owner = "rust/crates/vonk-agent/src/held_lock.rs"
    assert not check_source(owner, raw, {1}, modes=("locks",))
    test = "rust/crates/vonk-agent/tests/recipe_builder/base_images.rs"
    assert not check_source(test, raw, {1}, modes=("locks",))
    unit = "#[cfg(test)]\nmod tests {\n    fn t() {\n        " + raw + "    }\n}\n"
    assert not check_source(agent, unit, {4}, modes=("locks",))


def test_a_table_wide_lock_is_taken_only_by_its_owners():
    # The catalog collector's fence: refused by every writer in flight, it kept
    # 1,708 of 2,348 revisions in production (2026-11-04).
    fence = 'session.execute(text("LOCK TABLE jobs IN EXCLUSIVE MODE NOWAIT"))\n'
    collector = "control/src/vonk_control/catalog_revision_collection.py"
    assert check_source(collector, fence, {1}, modes=("locks",))
    assert check_source(
        collector, 'sql = "lock table users in share mode"\n', {1}, modes=("locks",)
    )
    assert not check_source(collector, fence, set(), modes=("locks",))
    owner = "control/src/vonk_control/browser_auth.py"
    assert not check_source(owner, fence, {1}, modes=("locks",))
    test = "control/tests/test_catalog_revision_collection.py"
    assert not check_source(test, fence, {1}, modes=("locks",))
    row = "with session.begin_nested():\n    session.execute(delete(Row))\n"
    assert not check_source(collector, row, {1, 2}, modes=("locks",))


@pytest.mark.parametrize(
    "source",
    [
        # The two shapes #1905 removed from the build failure summary.
        "tail = safe_text(diagnostics.stderr.text or diagnostics.stdout.text)\n",
        "kept = log_excerpt[:1024]\n",
        "shown = redact_text(stderr)[:512]\n",
        "text = diagnostics.stdout.text[:256]\n",
    ],
)
def test_output_bounded_from_its_front_or_one_preferred_stream_fails(source):
    path = "control/src/vonk_control/availability_production.py"
    assert check_source(path, source, {1}, modes=("excerpts",))
    assert not check_source(
        "control/tests/test_availability_production.py",
        source,
        {1},
        modes=("excerpts",),
    )


@pytest.mark.parametrize(
    "source",
    [
        "tail = log_excerpt[-1024:]\n",
        "detail = detail[:128]\n",
        "streams = (diagnostics.stdout.text, diagnostics.stderr.text)\n",
        # A raw tool's own error stream, not a retained tail.
        'detail = (result.stderr or result.stdout or "skopeo copy failed").strip()\n',
        "page = catalog[:20]\n",
        "shown = dialog[:3] + login[:8] + logs[:10]\n",
    ],
)
def test_output_kept_from_its_end_from_both_streams_is_allowed(source):
    path = "control/src/vonk_control/availability_production.py"
    assert not check_source(path, source, {1}, modes=("excerpts",))


# The CI failure on #1735: keep_default_key outlived 1 s on a loaded runner.
GATEWAY_WAIT = (
    "async def observe():\n"
    "    await asyncio.wait_for(\n"
    "        keep_default_key(\n"
    "            service,  # type: ignore[arg-type]\n"
    "            asyncio.Event(),\n"
    "            first_delay=0.001,\n"
    "            maximum_delay=0.002,\n"
    "        ),\n"
    "        timeout=1,\n"
    "    )\n"
)


@pytest.mark.parametrize(
    "source",
    [
        GATEWAY_WAIT,
        "assert executor.submit(planner.tick).result(timeout=1) is False\n",
        "assert entered.wait(2)\n",
        "process.communicate(timeout=5)\n",
        "subprocess.run(command, check=True, timeout=2)\n",
        "worker.join(timeout=3)\nassert not worker.is_alive()\n",
        "rendezvous = Rendezvous(2, timeout=1)\n",
        "deadline = time.monotonic() + 2\n",
        "LIMIT = 1\nfuture.result(LIMIT)\n",
        "@pytest.mark.timeout(3)\ndef test_x():\n    pass\n",
        "assert time.monotonic() - started < 0.5\n",
        "assert 0.3 <= elapsed < 2\n",
        # Popen.wait raises on expiry, even in a cleanup loop.
        "for child in children:\n    child.wait(timeout=5)\n",
        # The opt-out must name its reason.
        "future.result(timeout=1)  # wall-clock:\n",
        # A shortened production budget expires on a loaded runner.
        'monkeypatch.setattr(observation, "PROFILE_READ_STORAGE_SECONDS", 0.1)\n',
        'monkeypatch.setattr("vonk_control.owner.BUDGET_SECONDS", 2)\n',
        'with patch.object(owner, "_LOCK_BUDGET_SECONDS", 0.2):\n    pass\n',
    ],
)
def test_a_new_short_wall_clock_limit_on_work_fails(source):
    lines = set(range(1, len(source.splitlines()) + 1))
    path = "control/tests/test_example.py"
    assert check_source(path, source, lines, modes=("clocks",))
    # Only an added line can fail the change.
    unchanged = source + "x = 1\n"
    assert not check_source(path, unchanged, {len(lines) + 1}, modes=("clocks",))


@pytest.mark.parametrize(
    "source",
    [
        GATEWAY_WAIT.replace("timeout=1", "timeout=30"),
        "assert not done.wait(0.1)\n",
        "assert done.wait(timeout=0.1) is False\n",
        "while not stop.wait(1):\n    poll()\n",
        "for _ in range(3):\n    if stop.wait(0.05):\n        return\n",
        "future.result()\n",
        "assert 0.3 <= elapsed\n",
        "assert time.monotonic() - started < 30\n",
        "future.result(timeout=1)  # wall-clock: proves the stub's own deadline\n",
        'monkeypatch.setattr(route_runtime, "_PUBLICATION_LOCK_BUDGET_SECONDS", 0)\n',
        'monkeypatch.setattr(observation, "PROFILE_READ_STORAGE_SECONDS", 60)\n',
        'monkeypatch.setattr(store, "_RETRY_SECONDS", 0.01)  # wall-clock: interval\n',
    ],
)
def test_a_generous_bound_expected_expiry_or_named_reason_is_allowed(source):
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(
        "control/tests/test_example.py", source, lines, modes=("clocks",)
    )
    # Product code is held to its own wait rules, not this test rule.
    assert not check_source(PATH, "future.result(timeout=1)\n", {1}, modes=("clocks",))


@pytest.mark.parametrize(
    "ending",
    [
        "return None",
        "return replace(row, state=State.FAILED)",
        "row.next_action_at = None",
        "return fail(row)",
    ],
)
def test_count_cannot_end_standing_intent(ending: str) -> None:
    source = "def tick(row):\n    if row.retry_count >= 5:\n        " + ending + "\n"
    assert check_source(PATH, source, {2}, modes=("retries",))
    assert check_source(PATH, source, {3}, modes=("retries",))
    assert not check_source(PATH, source, {1}, modes=("retries",))


@pytest.mark.parametrize(
    "source",
    [
        "if row.repeat_count >= 5:\n    show_warning(row)\n",
        "if row.cancel_requested and row.observe_count >= STOP_BUDGET:\n    return replace(row, state=State.CANCELLED)\n",
        "if not ongoing_intent and failed_attempts >= self.max_failures:\n    return None\n",
        "if row.retry_count >= 5:\n    return replace(row, next_action_at=now + cooldown)\n",
        # A subscript target beside an attempt is not a counter of constants.
        "def run(attempt):\n    records[key] = attempt\n    if size == 0:\n        return None\n",
    ],
)
def test_count_guard_allows_evidence_backoff_cancel_and_request_local_retry(
    source: str,
) -> None:
    from .deterministic_retries import scan_count_endings

    assert not scan_count_endings(source)


def test_every_durable_recovery_policy_user_keeps_a_next_observation() -> None:
    from pathlib import Path

    from .deterministic_retries import scan_recovery_policy_users

    root = Path(__file__).resolve().parents[2]
    found = [
        f"{path.relative_to(root)}:{line}"
        for path in (root / "control/src").rglob("*.py")
        for line in scan_recovery_policy_users(
            path.read_text(), path=str(path.relative_to(root))
        )
    ]
    assert found == []


def test_guard_rejects_a_durable_policy_call_without_ongoing_intent() -> None:
    source = "due = RECOVERY.next_attempt(row.id, row.retry_count, now)\n"
    assert check_source(PATH, source, {1}, modes=("retries",))
    assert not check_source(PATH, source, set(), modes=("retries",))


@pytest.mark.parametrize(
    "source",
    [
        "count = row.retry_count + 1\nif count >= LIMIT:\n    return replace(row, state=State.FAILED)\n",
        "if RECOVERY.exhausted(repeats):\n    return None\n",
        "if not row.cancel_requested and row.retry_count >= 5:\n    return None\n",
        "if row.cancel_requested or row.retry_count >= 5:\n    return None\n",
        "seen[key] = attempt + 1\nif seen[key] >= 3:\n    return None\n",
        "seen, other = attempt + 1, 0\nif seen >= 3:\n    return None\n",
    ],
)
def test_count_guard_catches_aliases_and_does_not_treat_active_intent_as_cancelled(
    source: str,
) -> None:
    from .deterministic_retries import scan_count_endings

    assert scan_count_endings(source)


def test_request_local_retry_can_end_without_ending_durable_intent() -> None:
    from .deterministic_retries import scan_count_endings

    assert not scan_count_endings(
        "for attempt in range(3):\n    if attempt == 2:\n        return None\n"
    )
    assert scan_count_endings(
        "for attempt in range(3):\n    if attempt == 2:\n        return replace(row, state=State.FAILED)\n"
    )


def test_count_guard_does_not_exempt_the_uncancelled_else_branch() -> None:
    from .deterministic_retries import scan_count_endings

    assert scan_count_endings(
        "if row.cancel_requested:\n    return cancelled(row)\n"
        "else:\n    if row.retry_count >= 5:\n        return None\n"
    )


def test_assignment_based_retry_requires_the_cause_classifier():
    """Catches the worker's old assignment-based deterministic retry shield."""
    from .deterministic_retries import scan_source

    source = "def tick():\n try: work()\n except Exception as error:\n  self._retry_at[task] = now + 5\n"
    assert scan_source(source)
    assert not scan_source(
        source.replace(
            "  self._retry_at", "  verdict = disposition(error)\n  self._retry_at"
        )
    )


def test_monotonic_agent_progress_requires_attempt_key_on_added_lines():
    # Wrong implementation: the whole-tree rule exists but CI's patch gate
    # accepts a new lifetime monotonic rejection on agent ingress.
    source = 'def ingress(previous, current):\n    if current.completed_bytes < previous.completed_bytes:\n        raise ValueError("backwards")\n'
    path = "control/src/vonk_control/agent_jobs/example.py"
    assert check_source(path, source, {2, 3}, modes=("progress",))
    assert not check_source(path, source, {1}, modes=("progress",))


def test_document_hashing_stays_at_ingress_and_ignores_existing_calls():
    source = "from vonk_forge_contracts import document_sha256 as digest\nvalue = digest(document)\n"
    assert check_source(PATH, source, {2}, modes=("identity",))
    assert not check_source(PATH, source, {1}, modes=("identity",))
    assert not check_source(
        "control/src/vonk_control/catalog_service.py", source, {2}, modes=("identity",)
    )


@pytest.mark.parametrize(
    "source,line",
    [
        ("def online(node, now):\n    return node.last_seen_at > now\n", 2),
        (
            "def fresh(receipt, now):\n    received_at = receipt.received_at\n    return 0 <= now - received_at.timestamp() <= maximum_age\n",
            3,
        ),
        (
            (
                "def online(node, current):\n"
                "    seen = node.last_seen_at\n"
                "    if current - seen < timedelta(0):\n"
                "        return False\n"
            ),
            3,
        ),
        (
            (
                "def online(node, now):\n"
                "    age = now - node.last_seen_at\n"
                "    return 0 <= age < window\n"
            ),
            3,
        ),
        (
            (
                "def ready(heartbeat, now):\n"
                "    age = now - heartbeat.completed_at\n"
                "    return age < timedelta(0)\n"
                "# ControlProcessHeartbeat\n"
            ),
            3,
        ),
        (
            "def read(now):\n    return select(ControlProcessHeartbeat).where(ControlProcessHeartbeat.completed_at <= now)\n",
            2,
        ),
        (
            "def reason():\n    return NodeOfflineReason.LAST_SEEN_IN_FUTURE\n",
            2,
        ),
    ],
)
def test_added_line_guard_rejects_negative_controller_contact_age(source, line):
    """Catches restoring the offline rule through a renamed local age variable."""
    assert check_source(PATH, source, {line}, modes=("contact-clocks",))
    assert not check_source(PATH, source, {line + 1}, modes=("contact-clocks",))


def test_contact_clock_guard_preserves_staleness_and_security():
    source = (
        "def online(node, current):\n"
        "    seen = node.last_seen_at\n"
        "    return current - seen > window\n"
        "def certificate(cert, now):\n"
        "    return cert.not_before > now\n"
    )
    assert not check_source(PATH, source, set(range(1, 6)), modes=("contact-clocks",))


_FACTS = "# reads NodeInventorySnapshot\n"


@pytest.mark.parametrize(
    "source",
    [
        # Direct, aliased and wrapped agent stamps aged against the clock.
        "def f(row, now):\n    return now - row.observed_at > window\n",
        "def f(row, now):\n    seen = row.observed_at\n    return (now - seen).total_seconds()\n",
        "def f(row, current):\n    seen = _utc(row.observed_at)\n    return max(0.0, (current - seen).total_seconds())\n",
        "def f(snapshots, now):\n    for node, (free, observed) in snapshots:\n        if now - observed > MAX_AGE:\n            continue\n",
        # A timedelta comparison is freshness too; only the ingress handlers may.
        "def f(snapshot, now):\n    return now - snapshot.observed_at > timedelta(seconds=MAX)\n",
        "def f(row):\n    return datetime.now(UTC) - row.observed_at > window\n",
        # Cutoff forms, in both orientations and with a clock call.
        "def f(row, now):\n    return aware(row.observed_at) < now - MAX_AGE\n",
        "def f(row, cache):\n    return cache._clock() - MAX_AGE <= aware(row.observed_at)\n",
        "def f(row, now):\n    return now - MAX_AGE > row.observed_at\n",
    ],
)
def test_agent_stamp_freshness_is_rejected(source):
    """Catches ageing inventory or telemetry by the agent's own clock, in the
    forms that slipped past a literal ``now - x.observed_at`` match."""
    source = _FACTS + source
    lines = set(range(1, len(source.splitlines()) + 1))
    hits = check_source(PATH, source, lines, modes=("contact-clocks",))
    assert hits and all("use received_at" in text for text in hits)


@pytest.mark.parametrize(
    "source",
    [
        # Freshness from the Controller's receive time.
        "def f(row, now):\n    return (now - row.received_at).total_seconds() > window\n",
        "def f(row, now):\n    return aware(row.received_at) < now - MAX_AGE\n",
        # Ordering reports never reads the clock.
        "def f(a, b):\n    return a.observed_at >= b.observed_at\n",
        "def f(sample, since):\n    return sample.observed_at - since < GRACE\n",
        "def f(a, last):\n    return a.observed_at <= last\n",
        # A row read by an observed_at ordering is not a stamp.
        "def f(session, now):\n    row = session.scalar(select(X).order_by(X.observed_at.desc()))\n    return now - row.received_at\n",
    ],
)
def test_agent_stamp_guard_allows_receipt_and_ordering(source):
    source = _FACTS + source
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(PATH, source, lines, modes=("contact-clocks",))


def test_agent_stamp_guard_ignores_controller_written_progress_timestamps():
    """Progress ``observed_at`` is the Controller's own clock, not an agent's."""
    source = (
        "def due(previous, now):\n"
        "    observed = _timestamp(previous.observed_at)\n"
        "    return observed is None or (now - observed).total_seconds() >= 1\n"
    )
    assert not check_source(PATH, source, {1, 2, 3}, modes=("contact-clocks",))


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("control/src/vonk_control/agent_api/observations.py", "inventory"),
        ("control/src/vonk_control/telemetry.py", "_canonical_sample"),
    ],
)
def test_ingress_handlers_may_judge_an_agent_stamp_by_location(path, name):
    body = (
        f"{_FACTS}def make(now):\n    def {name}(v):\n"
        "        return now - v.observed_at > timedelta(minutes=5)\n"
    )
    assert not check_source(path, body, {3, 4}, modes=("contact-clocks",))
    assert check_source(
        "control/src/vonk_control/other.py", body, {3, 4}, modes=("contact-clocks",)
    )


@pytest.mark.parametrize("negative", (False, True))
@pytest.mark.parametrize("verdict", ("WAIT", "OBSERVE", "FAIL"))
@pytest.mark.parametrize("reason", ("wait_reason(error)", "str(error)", "None"))
def test_visible_classified_wait_is_not_counted_as_repeated_failure(
    negative, verdict, reason
):
    """Catches waits needing failure counts or logging a null/free-text reason."""
    typed = reason == "wait_reason(error)"
    check = (
        f"disposition(error) {'not in' if negative else 'in'} {{Disposition.{verdict}}}"
    )
    schedule = (
        "self._retry_at[task] = due\n"
        "log_event(logger, 'owner.wait', service='control-worker', "
        f"reason={reason}, retry_at=due)\n"
    )
    from textwrap import indent

    source = "try:\n    work()\nexcept Exception as error:\n"
    if negative:
        source += f"    if {check}:\n        raise\n" + indent(schedule, "    ")
    else:
        source += f"    if {check}:\n" + indent(schedule, "        ")
    hits = check_source(
        PATH, source, set(range(1, len(source.splitlines()) + 1)), modes=("repeats",)
    )
    assert bool(hits) == (not typed or verdict == "FAIL")


@pytest.mark.parametrize(
    "call",
    [
        "self._assessment_provider",
        "self._run_switch.inspect_request",
        "self._run_switch.recheck_resources_in_session",
        "service._lifecycle.preview_run",
        "self.preview",
        "self.assess",
        "self._replan_pending_application",
    ],
)
@pytest.mark.parametrize(
    "caught", ["ValueError", "(KeyError, TypeError, ValueError)", "Exception", ""]
)
def test_planner_defects_cannot_be_added_as_preparation_waits(call, caught):
    """Catch a new planner call or broad handler swallowing ValidationError."""
    handler = f"except {caught}:" if caught else "except:"
    source = f"try:\n    plan = {call}(request)\n{handler}\n    wait()\n"
    for line in (2, 3):
        assert check_source(PATH, source, {line}, modes=("planner-defects",))
    assert not check_source(PATH, source, {1}, modes=("planner-defects",))
    for verdict in (
        "raise planner_contract_defect(assignment.id, error) from error",
        "raise FleetProfileRecheckContractDefect('recheck failed') from error",
        "raise",
    ):
        separated = source.replace(
            handler,
            f"except ValidationError as error:\n    {verdict}\n" + handler,
        )
        assert not check_source(
            PATH, separated, set(range(1, 8)), modes=("planner-defects",)
        )


@pytest.mark.parametrize(
    "call", ["fleet_services.upgrades.preview", "service._mappings.preview"]
)
def test_planner_defect_guard_ignores_other_services_previews(call):
    """Catch the planner guard flagging another service's same-named preview."""
    source = f"try:\n    plan = {call}(request)\nexcept ValueError:\n    wait()\n"
    assert not check_source(PATH, source, {2, 3}, modes=("planner-defects",))


_CLAIM = "    claimed = cache._claim_operations(limit=requested)\n"


@pytest.mark.parametrize(
    "gate",
    [
        # The main-era model-cache tick: a full pool skipped the claim, so a
        # download whose bytes were all stored waited behind a hung transfer.
        "    capacity = cache._available_transfer_slots()\n    if not capacity:\n        return 0\n",
        "    if slots <= 0:\n        return 0\n",
        "    if self.free_workers == 0:\n        return 0\n",
        "    if available < 1:\n        return 0\n",
        "    if not cache._available_transfer_slots():\n        return 0\n",
        "    if len(inflight) >= self._parallelism:\n        return 0\n",
    ],
)
def test_capacity_gate_ahead_of_a_claim_is_flagged(gate: str) -> None:
    source = "def tick(cache):\n" + gate + _CLAIM
    gate_line = next(n for n, t in enumerate(source.splitlines(), 1) if "if " in t)
    assert check_source(PATH, source, {gate_line}, modes=("capacity-gates",))
    submit = source.replace(_CLAIM, "    pool.submit(run)\n")
    assert check_source(PATH, submit, {gate_line}, modes=("capacity-gates",))


@pytest.mark.parametrize(
    "source",
    [
        # Claim first; only the transfer waits for a slot.
        "def tick(cache):\n" + _CLAIM + "    if not capacity:\n        return 0\n",
        # A gate that does not stop the function before its claim.
        "def tick(cache):\n    if not capacity:\n        note_full()\n" + _CLAIM,
        # No claim or submit in the function.
        "def tick(cache):\n    if not capacity:\n        return 0\n",
        # A capacity read that is not a full-pool test.
        "def tick(cache):\n    if capacity > 0:\n        return 0\n" + _CLAIM,
    ],
)
def test_capacity_guard_allows_claim_first_and_non_gates(source: str) -> None:
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(PATH, source, lines, modes=("capacity-gates",))


def test_capacity_guard_allows_reviewed_gates_and_tests() -> None:
    source = "def tick(self):\n    if capacity <= 0:\n        return 0\n" + _CLAIM
    reviewed = "control/src/vonk_control/availability_production.py"
    assert not check_source(reviewed, source, {2}, modes=("capacity-gates",))
    test = "control/tests/test_example.py"
    assert not check_source(test, source, {2}, modes=("capacity-gates",))


@pytest.mark.parametrize(
    "body",
    (
        # The main-era exact-recovery shape (#1896).
        "    return parent.start_deadline - start.created_at\n",
        "    return _aware(parent.start_deadline) - _aware(row.created_at)\n",
        '    return row["start_anchored_at"] - row["created_at"]\n',
        (
            "    created = job.created_at\n"
            "    return _aware(_parent_start_deadline(job)) - created\n"
        ),
    ),
)
def test_added_line_guard_rejects_start_budget_from_stored_timestamps(body):
    """A budget re-derived from an anchored deadline drifts from the accepted one."""
    source = "def budget(parent, start, row, job):\n" + body
    last = source.count("\n")
    assert check_source(PATH, source, {last}, modes=("start-budgets",))
    assert not check_source(PATH, source, {1}, modes=("start-budgets",))


def test_plan_digest_guard_flags_reads_of_the_replanned_plan_only():
    source = (
        "def start(service, session, plan, run_id):\n"
        "    if plan.plan_digest != reviewed:\n"
        "        raise ValueError\n"
        "    run_id = service.accept_run_in_session(session, plan)\n"
        "    run = session.get(RecipeRun, run_id)\n"
        "    stale = plan.plan_digest\n"
        "    saved = run.plan_digest\n"
        "def other(plan):\n"
        "    return plan.plan_digest\n"
    )
    modes = ("plan-digests",)
    assert check_source(PATH, source, {6}, modes=modes)
    assert not check_source(PATH, source, {2, 7, 9}, modes=modes)
    assert not check_source("control/tests/test_example.py", source, {6}, modes=modes)


def test_start_budget_guard_allows_reviewed_sites_settings_and_other_clocks():
    source = (
        "def _anchor_start_budget(session, operation, now):\n"
        "    deadline = _operation_start_deadline(operation)\n"
        "    return _aware(deadline) - _aware(operation.created_at)\n"
    )
    reviewed = "control/src/vonk_control/agent_jobs/predicates.py"
    assert check_source(PATH, source, {3}, modes=("start-budgets",))
    assert not check_source(reviewed, source, {3}, modes=("start-budgets",))
    test = "control/tests/test_example.py"
    assert not check_source(test, source, {3}, modes=("start-budgets",))
    other = (
        "def remaining(parent, now, job):\n"
        "    left = parent.start_deadline - now\n"
        "    age = now - job.created_at\n"
        "    return timedelta(seconds=settings.DISTRIBUTED_START_TIMEOUT_SECONDS)\n"
    )
    assert not check_source(PATH, other, set(range(1, 5)), modes=("start-budgets",))


@pytest.mark.parametrize(
    "text",
    [
        'artifacts = _gh("api", f"repos/{repo}/actions/runs/{run_id}/artifacts")\n',
        "gh api repos/$REPO/actions/runs/$RUN/artifacts --jq '.artifacts[]'\n",
        "gh api repos/$REPO/actions/artifacts\n",
    ],
)
def test_added_line_guard_rejects_a_one_page_artifact_listing(text):
    """A run holds more artifacts than one default page (#1900)."""
    path = "scripts/example.sh"
    assert check_source(path, text, {1}, modes=("listings",))
    assert not check_source(path, text, {2}, modes=("listings",))
    assert not check_source("tests/example.sh", text, {1}, modes=("listings",))


@pytest.mark.parametrize(
    "text",
    [
        "gh api --paginate repos/$REPO/actions/runs/$RUN/artifacts\n",
        'f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"\n',
        'f"repos/{repo}/actions/runs/{run_id}/artifacts"\n    f"?per_page=100&page={page}"\n',
        "gh api repos/$REPO/actions/artifacts?name=agent-package\n",
        "gh api repos/$REPO/actions/artifacts/$ID/zip > artifact.zip\n",
    ],
)
def test_added_line_guard_allows_paged_or_named_artifact_listings(text):
    assert not check_source("scripts/example.sh", text, {1}, modes=("listings",))


def test_added_line_guard_allows_a_paginating_wrapper_and_workflow_continuations():
    wrapper = (
        "def gh(endpoint):\n"
        '    return run(["gh", "api", "--paginate", "--slurp", endpoint])\n'
        'pages = gh(f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")\n'
    )
    assert not check_source("scripts/example.py", wrapper, {3}, modes=("listings",))
    workflow = (
        "        run: |\n"
        "          gh api --paginate \\\n"
        "            -H 'Accept: application/vnd.github+json' \\\n"
        "            -H 'X-GitHub-Api-Version: 2022-11-28' \\\n"
        "            -f per_page=100 \\\n"
        '            "/repos/$REPO/actions/runs/$RUN/artifacts" --jq .artifacts[]\n'
    )
    path = ".github/workflows/example.yml"
    assert not check_source(path, workflow, {6}, modes=("listings",))
    one_page = workflow.replace("--paginate ", "").replace("per_page", "x")
    assert check_source(path, one_page, {6}, modes=("listings",))


_PAGING_WRAPPER = (
    "def gh(endpoint):\n"
    '    return run(["gh", "api", "--paginate", "--slurp", endpoint])\n'
    "\n"
    "\n"
)


def test_added_line_guard_scopes_the_wrapper_and_flags_a_bare_trailing_slash():
    direct = (
        _PAGING_WRAPPER
        + "pass\n" * 5
        + 'run(["gh", "api", f"repos/o/r/actions/runs/{run_id}/artifacts"])\n'
    )
    last = direct.count("\n")
    assert check_source("scripts/example.py", direct, {last}, modes=("listings",))
    through = (
        _PAGING_WRAPPER + 'pages = gh(f"/repos/o/r/actions/runs/{run_id}/artifacts")\n'
    )
    last = through.count("\n")
    assert not check_source("scripts/example.py", through, {last}, modes=("listings",))
    slash = 'gh(f"repos/o/r/actions/runs/{run_id}/artifacts/")\n'
    assert check_source("scripts/example.py", slash, {1}, modes=("listings",))


def test_added_line_guard_needs_the_wrapper_itself_to_paginate():
    per_call = (
        "def gh(*args):\n"
        '    return run(["gh", "api", *args])\n'
        'runs = gh("--paginate", "repos/o/r/actions/runs")\n'
        + "pass\n" * 5
        + 'listing = gh(f"/repos/o/r/actions/runs/{run_id}/artifacts")\n'
    )
    assert check_source("scripts/example.py", per_call, {9}, modes=("listings",))
    original = (
        'flags = ["--paginate"]\n'
        + "pass\n" * 5
        + 'artifacts = _gh("api", f"repos/{repo}/actions/runs/{run_id}/artifacts")\n'
    )
    assert check_source("scripts/example.py", original, {7}, modes=("listings",))


def test_added_line_guard_checks_a_direct_multiline_call_in_a_paginating_file():
    source = (
        _PAGING_WRAPPER + "pass\n" * 5 + "subprocess.run(\n"
        "    [\n"
        '        "gh",\n'
        '        "api",\n'
        '        f"/repos/o/r/actions/runs/{run_id}/artifacts",\n'
        "    ]\n"
        ")\n" + 'url = f"/repos/o/r/actions/runs/{run_id}/artifacts"\n'
    )
    rows = source.splitlines()
    for line, text in enumerate(rows, 1):
        if "artifacts" in text:
            assert check_source(
                "scripts/example.py", source, {line}, modes=("listings",)
            )


def test_the_classified_pairwise_progress_check_is_allowed():
    source = (
        "def progress_advanced(previous, current):\n"
        "    return current.phase != previous.phase\n"
    )
    assert not check_source(
        "control/src/vonk_control/stall_window.py", source, {2}, modes=("movement",)
    )
    assert check_source(
        "control/src/vonk_control/other.py", source, {2}, modes=("movement",)
    )


@pytest.mark.parametrize(
    "source",
    [
        # The literal parse #1906 removed from the job failure.
        '.any(|token| token == "exit_cause=host_memory_exhausted")\n',
        'if detail.contains("exit_cause=oom_killed") {\n',
        'Some(cause) if cause == "python_exception" => {}\n',
    ],
)
def test_a_new_exit_cause_literal_fails_outside_its_owner(source):
    agent = "rust/crates/vonk-agent/src/executor/jobs.rs"
    assert check_source(agent, source, {1}, modes=("exit-causes",))
    # The enum spells its own tokens; tests may build any summary.
    assert not check_source(
        "rust/crates/vonk-agent-protocol/src/failure_evidence.rs",
        source,
        {1},
        modes=("exit-causes",),
    )
    unit = "#[cfg(test)]\nmod tests {\n    fn t() {\n        " + source + "    }\n}\n"
    assert not check_source(agent, unit, {4}, modes=("exit-causes",))
    typed = (
        "if ExitCause::of_summary(detail) == Some(ExitCause::HostMemoryExhausted) {\n"
    )
    assert not check_source(agent, typed, {1}, modes=("exit-causes",))


def test_a_new_exit_cause_literal_fails_in_controller_code():
    path = "control/src/vonk_control/agent_api/observations.py"
    for source in (
        'if item.value == "host_memory_exhausted":\n    pass\n',
        'if "exit_cause=oom_killed" in detail:\n    pass\n',
    ):
        assert check_source(path, source, {1}, modes=("exit-causes",))
    assert not check_source(
        path, 'if item.name == "trigger":\n    pass\n', {1}, modes=("exit-causes",)
    )


_SPACE_CHECK = "def fits(cache, needed):\n    return needed <= cache.free_bytes()\n"


def test_nas_space_read_outside_the_storage_owner_is_flagged() -> None:
    path = "control/src/vonk_control/model_cache/download_admission.py"
    assert check_source(path, _SPACE_CHECK, {2}, modes=("nas-space",))
    raw = "def fits(root):\n    return shutil.disk_usage(root).free\n"
    assert check_source(path, raw, {2}, modes=("nas-space",))
    assert not check_source(path, _SPACE_CHECK, {1}, modes=("nas-space",))


def test_the_storage_owner_and_other_packages_may_read_nas_space() -> None:
    for path in (
        "control/src/vonk_control/model_cache/storage.py",
        "control/src/vonk_control/unused_storage_collection/pressure.py",
        "control/tests/test_model_cache.py",
    ):
        assert not check_source(path, _SPACE_CHECK, {2}, modes=("nas-space",))


@pytest.mark.parametrize(
    "source",
    [
        "MODEL_CACHE_RESERVE_BYTES = 10 * 1024**3\n",
        "reserve_bytes: int = 10 * 1024**3\n",
        "_MODEL_CACHE_RESERVE_BYTES = 10 * 1024**3\n",
        "NAS_RESERVE_BYTES = (4 << 30)\n",
        "X_RESERVE_BYTES = OTHER_CONSTANT\n",
        "def f(self, reserve_bytes: int = 5 * 1024**3): ...\n",
    ],
)
def test_a_fixed_byte_reserve_is_flagged(source: str) -> None:
    path = "control/src/vonk_control/settings.py"
    assert check_source(path, source, {1}, modes=("fixed-reserves",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (
            "control/src/vonk_control/recipe_builds/common.py",
            "MINIMUM_BUILD_DISK_RESERVE_BYTES = 4 * 1024**3\n",
        ),
        ("control/src/vonk_control/other.py", "RESERVE_FRACTION = 0.1\n"),
        (
            "control/src/vonk_control/other.py",
            "reserve_bytes=cache.reserve_of(total)\n",
        ),
        ("control/src/vonk_control/other.py", "reserve_bytes: int = Field(ge=0)\n"),
        ("control/src/vonk_control/other.py", "reserved_bytes = 4096\n"),
        ("control/tests/test_x.py", "RESERVE_BYTES = 10 * 1024**3\n"),
    ],
)
def test_other_reserve_lines_are_not_flagged(path: str, source: str) -> None:
    assert not check_source(path, source, {1}, modes=("fixed-reserves",))


_SET_SELECTION = (
    "def sets(session, digest):\n"
    "    return session.scalars(\n"
    "        select(ModelCacheSet.artifact_set_sha256).where(\n"
    "            ModelCacheSet.model_content_sha256 == digest\n"
    "        )\n"
    "    )\n"
)


@pytest.mark.parametrize(
    "path",
    [
        "control/src/vonk_control/model_cache/removal_review.py",
        "control/src/vonk_control/model_cache/fleet_removal.py",
    ],
)
def test_removal_choosing_sets_by_first_requester_is_flagged(path: str) -> None:
    assert check_source(path, _SET_SELECTION, {4}, modes=("model-sets",))
    assert not check_source(path, _SET_SELECTION, {1}, modes=("model-sets",))


def test_the_shared_resolver_and_other_modules_may_select_by_digest() -> None:
    for path in (
        "control/src/vonk_control/model_cache/lineage.py",
        "control/src/vonk_control/model_cache/resolution.py",
        "control/tests/test_model_removal_lineage_postgres.py",
    ):
        assert not check_source(path, _SET_SELECTION, {4}, modes=("model-sets",))


_WHOLE_MEMBERSHIP = (
    "def read(session, sets):\n"
    "    every = session.scalars(select(ModelCacheSetArtifact.artifact_sha256))\n"
    "    named = session.scalars(\n"
    "        select(ModelCacheSetArtifact.artifact_sha256).where(\n"
    "            ModelCacheSetArtifact.artifact_set_sha256.in_(sets)\n"
    "        )\n"
    "    )\n"
    "    return every, named\n"
)


_DELETED_ACCEPTANCE_READ = (
    "def read(session, selected):\n"
    "    return session.scalars(\n"
    "        select(ModelCacheSetArtifact.artifact_sha256).where(\n"
    "            ModelCacheSetArtifact.artifact_set_sha256.not_in(selected)\n"
    "        )\n"
    "    )\n"
)
_QUERY_ALL = (
    "def read(session):\n    return session.query(ModelCacheSetArtifact).all()\n"
)
_TARGETED_FILTER = (
    "def read(session, sets):\n"
    "    return session.query(ModelCacheSetArtifact).filter(\n"
    "        ModelCacheSetArtifact.artifact_set_sha256.in_(sets)\n"
    "    ).all()\n"
)
_MEMBERSHIP_PATHS = (
    "control/src/vonk_control/model_cache/removal_acceptance.py",
    "control/src/vonk_control/artifact_reference_scan/model_sets.py",
    "control/src/vonk_control/unused_storage_collection/service.py",
)


@pytest.mark.parametrize("path", _MEMBERSHIP_PATHS)
def test_a_whole_membership_table_read_is_flagged(path: str) -> None:
    whole = "def r(s):\n    return s.scalars(select(ModelCacheSetArtifact))\n"
    assert check_source(path, _DELETED_ACCEPTANCE_READ, {3}, modes=("model-sets",))
    assert check_source(path, _QUERY_ALL, {2}, modes=("model-sets",))
    assert check_source(path, whole, {2}, modes=("model-sets",))
    assert not check_source(path, _DELETED_ACCEPTANCE_READ, {1}, modes=("model-sets",))


@pytest.mark.parametrize("path", _MEMBERSHIP_PATHS)
def test_a_membership_read_that_names_rows_passes(path: str) -> None:
    named = (
        "def r(s, sets):\n"
        "    return s.scalars(select(ModelCacheSetArtifact.artifact_sha256).where(\n"
        "        ModelCacheSetArtifact.artifact_set_sha256.in_(sets)))\n"
    )
    limited = "def r(s):\n    return s.scalar(select(ModelCacheSetArtifact).limit(1))\n"
    for source in (_TARGETED_FILTER, named, limited):
        assert not check_source(path, source, {2, 3}, modes=("model-sets",))
    assert not check_source(
        "control/tests/test_x.py", _QUERY_ALL, {2}, modes=("model-sets",)
    )


_DIRECT_OPENER = (
    "def check(cache, client):\n    return cache._open_http_response(client, URL, {})\n"
)


@pytest.mark.parametrize(
    "call",
    [
        "cache._open_http_response(client, URL, {})",
        "cache._open_github_release_asset(client, spec, {})",
        "cache._open_github_lfs_object(client, spec, {})",
        "provider_for(spec.kind).open(cache, client, spec, {})",
        "PROVIDERS[spec.kind].open(cache, client, spec, {})",
    ],
)
def test_a_provider_opened_past_the_one_opener_is_flagged(call: str) -> None:
    source = f"def check(cache, client, spec):\n    return {call}\n"
    for path in (
        "control/src/vonk_control/model_cache/updates.py",
        "control/src/vonk_control/model_cache/download.py",
    ):
        assert check_source(path, source, {2}, modes=("openers",)), (path, call)
    assert not check_source(
        "control/src/vonk_control/model_cache/updates.py",
        source,
        {1},
        modes=("openers",),
    )


def test_the_provider_table_and_the_one_opener_may_open_providers() -> None:
    opener = (
        "def _open_provider(self, client, spec, headers):\n"
        "    return provider_for(spec.kind).open(self, client, spec, headers)\n"
    )
    assert not check_source(
        "control/src/vonk_control/model_cache/download.py",
        opener,
        {2},
        modes=("openers",),
    )
    assert not check_source(
        "control/src/vonk_control/model_cache/providers.py",
        _DIRECT_OPENER,
        {2},
        modes=("openers",),
    )
    assert not check_source(
        "control/tests/test_model_cache_huggingface_auth.py",
        _DIRECT_OPENER,
        {2},
        modes=("openers",),
    )
    definition = "def _open_http_response(self, client, source, headers):\n    pass\n"
    assert not check_source(
        "control/src/vonk_control/model_cache/http.py",
        definition,
        {1},
        modes=("openers",),
    )


def test_the_anonymous_github_primitive_is_for_the_github_modules_only() -> None:
    source = "def check(cache, client):\n    return cache._send_anonymous_github_request(client, URL)\n"
    assert check_source(
        "control/src/vonk_control/model_cache/updates.py",
        source,
        {2},
        modes=("openers",),
    )
    for module in ("github.py", "github_lfs.py"):
        assert not check_source(
            f"control/src/vonk_control/model_cache/{module}",
            source,
            {2},
            modes=("openers",),
        )


_FOLLOW_COUNT = (
    "def same_job(observed):\n"
    "    active = [item for item in observed['operations']]\n"
    "    if len(active) > 1:\n"
    "        raise ControlMalformedResponse('more than one active Spark')\n"
    "    if observed['progress']['running'] > 1:\n"
    "        raise ControlMalformedResponse('concurrent upgrades')\n"
)


def test_follow_validator_asserting_an_operation_count_is_flagged() -> None:
    """Catches a follow validator that rejects parallel Spark upgrades."""
    path = "src/cluster_profiles/controller_cli/fleet.py"
    assert check_source(path, _FOLLOW_COUNT, {3}, modes=("follow-validators",))
    assert check_source(path, _FOLLOW_COUNT, {5}, modes=("follow-validators",))
    assert not check_source(path, _FOLLOW_COUNT, {1, 2}, modes=("follow-validators",))
    assert not check_source(
        "src/cluster_profiles/other.py",
        _FOLLOW_COUNT,
        {3},
        modes=("follow-validators",),
    )


_DOCUMENT_READS = (
    "release = document.get('release')\n",
    "name = row.document['identity']\n",
    "slug = entry['document']['identity']['slug']\n",
    "runtime = json.loads(files['recipe.json'])['runtime']\n",
    "def f(row):\n    bound = row.document\n    return bound['models']\n",
)


@pytest.mark.parametrize("source", _DOCUMENT_READS)
def test_added_raw_document_read_fails_untouched_read_does_not(source: str) -> None:
    path = "control/src/vonk_control/example.py"
    last = source.count("\n")
    assert check_source(
        path, source, set(range(1, last + 1)), modes=("document-reads",)
    )
    assert not check_source(path, source, set(), modes=("document-reads",))
    assert not check_source(
        "src/cluster_profiles/generated_control/x.py",
        source,
        {1},
        modes=("document-reads",),
    )
    assert not check_source(
        "control/tests/example.py", source, {1}, modes=("document-reads",)
    )
    assert not check_source(
        "agent_protocol/src/example.py", source, {1}, modes=("document-reads",)
    )


@pytest.mark.parametrize(
    "source",
    [
        "release = read_recipe(row.document).release\n",
        "runtime = payload.get('runtime')\n",
        "title = display_model.title\n",
        "name = document['unlisted_key']\n",
    ],
)
def test_typed_or_unrelated_read_passes_document_reads(source: str) -> None:
    assert not check_source(
        "control/src/vonk_control/example.py", source, {1}, modes=("document-reads",)
    )


_ITEM_COUNT = (
    "class Plan(Model):\n"
    "    artifact_digests: list[Digest] = Field(default_factory=list, max_length=256)\n"
    "    objects: tuple[Obj, ...] = Field(\n"
    "        min_length=1,\n"
    "        max_length=4096,\n"
    "    )\n"
    "    files: list[F] = Field(max_length=8)  # item-count-limit: wire frame\n"
    "    node_ids: list[str] = Field(max_length=32)\n"
    "    digest: str = Field(max_length=64)\n"
    "    input_files: list[In] = Field(max_length=MAX_INPUT_FILES)\n"
    "    artifact_count: int = Field(ge=1, le=1024)\n"
    "    artifact_bytes: int = Field(ge=1, le=1024)\n"
    "    object_ids: list[str] = Field(max_length=MAX_FLEET)\n"
    "    profile_count: int = Field(ge=0, le=MAX_DATABASE_INTEGER)\n"
    "    rooms: list[str] = Field(max_length=MAX_DATABASE_INTEGER)\n"
)


def test_item_count_cap_on_a_model_sized_list_is_flagged() -> None:
    """Catches a digest/object cap like the 256-digest one that failed a plan."""
    path = "control/src/vonk_control/plan_contract.py"
    assert check_source(path, _ITEM_COUNT, {2}, modes=("item-counts",))
    assert check_source(path, _ITEM_COUNT, {5}, modes=("item-counts",))
    assert not check_source(path, _ITEM_COUNT, {7, 8, 9}, modes=("item-counts",))
    assert not check_source(path, _ITEM_COUNT, {1}, modes=("item-counts",))
    # A named constant is a count too; so is ``le`` on a count field.
    assert check_source(path, _ITEM_COUNT, {10}, modes=("item-counts",))
    assert check_source(path, _ITEM_COUNT, {11}, modes=("item-counts",))
    assert not check_source(path, _ITEM_COUNT, {12}, modes=("item-counts",))
    # Whole name tokens only: ``object_ids`` has the token ``object``; a database
    # range constant and names without a model-sized token are not counts.
    assert check_source(path, _ITEM_COUNT, {13}, modes=("item-counts",))
    assert not check_source(path, _ITEM_COUNT, {14, 15}, modes=("item-counts",))


@pytest.mark.parametrize(
    "call",
    [
        "asyncio.run(main())",
        "asyncio.Runner()",
        "uvloop.run(main())",
        "anyio.run(main)",
        "loop.run_until_complete(main())",
    ],
)
def test_an_event_loop_entered_outside_the_transport_is_flagged(call: str) -> None:
    source = f"def go(main, loop):\n    return {call}\n"
    path = "src/cluster_profiles/cli.py"
    assert check_source(path, source, {2}, modes=("event-loops",)), call
    # The transport owns the entry; other trees and old lines stay quiet.
    owner = "src/cluster_profiles/control_transport.py"
    assert not check_source(owner, source, {2}, modes=("event-loops",))
    assert not check_source(
        "control/src/vonk_control/x.py", source, {2}, modes=("event-loops",)
    )
    assert not check_source(path, source, {1}, modes=("event-loops",))
    quiet = "def go(runner, work):\n    return runner.run(work)  # asyncio.run(x)\n"
    assert not check_source(path, quiet, {2}, modes=("event-loops",))


# The three runtime caps the model-cache change deleted, line for line (main's
# model_cache/artifacts.py:395 and :420, catalog_helpers.py:368), and the attribute
# form of a live one (control source_bundles.py:509).
_RUNTIME_CAPS = (
    "def parts(parts):\n"
    "    return (\n"
    "        not 2 <= len(parts) <= _MAX_ARTIFACT_PARTS\n"
    "    )\n"
    "\n"
    "\n"
    "def manifest(value):\n"
    "    if len(value.artifacts) < 1 or len(value.artifacts) > _MAX_ARTIFACTS:\n"
    "        raise ValueError\n"
    "\n"
    "\n"
    "def models(result):\n"
    "    if len(result) > _MAX_ARTIFACTS:\n"
    "        raise ValueError\n"
    "    if len(result) < 2 or len(result) > MAX_NODES or len(result) > 3:\n"
    "        raise ValueError\n"
    "\n"
    "\n"
    "def bundle(files, limits):\n"
    "    if len(files) >= limits.max_files:\n"
    "        raise ValueError\n"
    "    if len(nodes) >= limits.max_nodes:\n"
    "        raise ValueError\n"
)


def test_runtime_len_caps_on_model_sized_collections_are_flagged() -> None:
    """Catches the three runtime ``len(...)`` caps the model-cache change deleted."""
    path = "control/src/vonk_control/model_cache/x.py"
    for line in (3, 8, 13):
        assert check_source(path, _RUNTIME_CAPS, {line}, modes=("item-counts",)), line
    # A small literal or a name with no model-sized word is a shape check.
    # ``limits.max_files`` is the same cap behind an attribute (source_bundles.py:509).
    assert check_source(path, _RUNTIME_CAPS, {20}, modes=("item-counts",))
    assert not check_source(path, _RUNTIME_CAPS, {15, 22}, modes=("item-counts",))
    assert not check_source(path, _RUNTIME_CAPS, {1, 2, 7}, modes=("item-counts",))
    lt = "class P(M):\n    artifact_count: int = Field(lt=1024)\n"
    assert check_source(path, lt, {2}, modes=("item-counts",))


TRAP_PATH = "deploy/compose/postgres/entrypoint.sh"


@pytest.mark.parametrize(
    "source",
    [
        # The backup worker's old copy poll.
        "trap 'cleanup; exit 0' TERM INT\nwhile kill -0 \"$pid\"; do\n  sleep 5\ndone\n",
        # The Tailscale reconciler's old sleep.
        'trap shutdown_services 1 2 15\nwhile :; do\n    sleep "${reconcile_interval}"\ndone\n',
        "trap -- 'cleanup' TERM\nsleep 5 && true\n",
        "trap cleanup TERM\nsleep 5 &\ntrue\n",
    ],
)
def test_a_foreground_sleep_under_a_termination_trap_fails(source):
    line = next(
        number for number, text in enumerate(source.splitlines(), 1) if "sleep" in text
    )
    assert check_source(TRAP_PATH, source, {line}, modes=("trap-waits",))
    # Only an added line can fail the change.
    assert not check_source(TRAP_PATH, source, {line + 1}, modes=("trap-waits",))


@pytest.mark.parametrize(
    "source",
    [
        "trap 'cleanup; exit 0' TERM INT\nsleep 5 & wait $!\n",
        'trap shutdown_services 1 2 15\nsleep "${reconcile_interval}" & wait $!\n',
        "trap cleanup 0\nsleep 5\n",
        "sleep 5\n",
        "trap 'cleanup' TERM\n# sleep 5\n",
        'trap cleanup TERM\nsleep 5 & wait "$!"\n',
        'trap cleanup TERM\nsleep 5 & pid=$!; wait "$pid"\n',
        "trap cleanup TERM\nsleep 5 &\nwait $!\n",
        "trap cleanup TERM\necho sleep 1\n",
        "trap - TERM\nsleep 5\n",
    ],
)
def test_interruptible_or_untrapped_sleeps_pass(source):
    lines = set(range(1, len(source.splitlines()) + 1))
    assert not check_source(TRAP_PATH, source, lines, modes=("trap-waits",))


def test_background_owners_may_not_add_request_retry_calls():
    source = "def scan(cache):\n    return cache.resolve_artifact_set(model_content_sha256=d)\n"
    modes = ("background-retries",)
    owner = "control/src/vonk_control/model_cache/revisions.py"
    request = "control/src/vonk_control/model_cache/download_admission.py"
    assert check_source(owner, source, {2}, modes=modes)
    assert check_source(
        owner, "for _a in bounded_attempts():\n    pass\n", {1}, modes=modes
    )
    assert not check_source(owner, source, {1}, modes=modes)  # not an added line
    assert not check_source(request, source, {2}, modes=modes)
    routes = "control/src/vonk_control/recipe_routes/withdrawal.py"
    assert check_source(
        routes, "for _a in bounded_attempts():\n    pass\n", {1}, modes=modes
    )
    service = "control/src/vonk_control/recipe_routes/service.py"
    assert check_source(
        service, "for _a in bounded_attempts():\n    pass\n", {1}, modes=modes
    )
    assert not check_source(
        owner,
        "cache._resolve_artifact_set_once(model_content_sha256=d)\n",
        {1},
        modes=modes,
    )


TICK_POLL_PATH = "control/tests/test_model_cache_example.py"
TICK_POLL_SETUP = "service = ModelCacheService(sessions, root)\n"
# The pre-fix resume-access receipt test (#2076).
FIXED_TICKS_THEN_OUTCOME = """\
for _ in range(4):
    service.tick()
    now[0] += timedelta(seconds=10)
assert service.get_operation(older.id).state in {
    LifecycleState.QUEUED,
    LifecycleState.BACKOFF,
}
failure = service.get_operation(newer.id).failure
assert failure is not None
"""
# A poll capped by a count and a sleep (pool liveness, availability production).
COUNTED_POLL = """\
for _ in range(3):
    service.tick()
    if service.get_operation(small.id).state == LifecycleState.SUCCEEDED:
        break
    time.sleep(0.05)
assert service.get_operation(small.id).state == LifecycleState.SUCCEEDED
"""
# Nothing may happen before the retry is due: the claim decides that
# synchronously (availability regressions, access recovery postgres).
TICKS_BEFORE_DUE = """\
for _ in range(3):
    service.tick()
assert len(requests) == attempts
"""
# Ticks before the next attempt consume none (pool liveness).
TICKS_KEEP_ATTEMPT = """\
for _ in range(5):
    service.tick()
assert service.get_operation(operation.id).attempt == waited.attempt
"""
# A worker reports QUEUED too (an access wait, a rate limit): the flake's
# exact form when only that state is asserted, and its negation.
TICKS_THEN_QUEUED = """\
for _ in range(4):
    service.tick()
assert service.get_operation(older.id).state == LifecycleState.QUEUED
"""
TICKS_THEN_NOT_QUEUED = """\
for _ in range(4):
    service.tick()
assert service.get_operation(newer.id).state != LifecycleState.QUEUED
"""
# A count-capped poll without a sleep is the same class.
SLEEPLESS_POLL = """\
for _ in range(100):
    service.tick()
    if service.get_operation(first.id).state == "queued":
        break
"""
# The operator readers read the same rows.
TICKS_THEN_OPERATOR_READ = """\
for _ in range(3):
    service.tick()
assert service.get_operator_operation(operation.id).state == "succeeded"
"""
# The read may follow an unrelated assertion.
TICKS_THEN_LATER_READ = """\
for _ in range(3):
    service.tick()
assert len(requests) == 2
assert service.get_operation(operation.id).state == LifecycleState.SUCCEEDED
"""
# A fixed number of cycles around a deadline-bounded wait.
CYCLES_OF_BOUNDED_WAITS = """\
for cycle in range(5):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        service.tick()
        if service.get_operation(waiting.id).failure:
            break
        time.sleep(0.01)
    assert service.get_operation(waiting.id).failure is not None
"""
# A synchronous service's ticks, in a module that also builds the cache.
SYNCHRONOUS_TICKS = """\
for _ in range(3):
    switches.tick()
assert switches.get(operation_id).state == LifecycleState.SUCCEEDED
"""


def _tick_poll_hits(body: str, *, setup: str = TICK_POLL_SETUP) -> list[str]:
    source = setup + body
    lines = set(range(1, len(source.splitlines()) + 1))
    return check_source(TICK_POLL_PATH, source, lines, modes=("tick-polls",))


@pytest.mark.parametrize(
    "body",
    [
        FIXED_TICKS_THEN_OUTCOME,
        COUNTED_POLL,
        TICKS_THEN_QUEUED,
        TICKS_THEN_NOT_QUEUED,
        SLEEPLESS_POLL,
        TICKS_THEN_OPERATOR_READ,
        TICKS_THEN_LATER_READ,
    ],
)
def test_fixed_tick_counts_cannot_wait_for_a_threaded_cache_outcome(body):
    assert _tick_poll_hits(body)
    # Only an added loop is reported, and only in a module that builds the cache.
    source = TICK_POLL_SETUP + body
    assert not check_source(TICK_POLL_PATH, source, {1}, modes=("tick-polls",))
    assert not _tick_poll_hits(body, setup="service = RunSwitchService(sessions)\n")


@pytest.mark.parametrize(
    "body",
    [
        TICKS_BEFORE_DUE,
        TICKS_KEEP_ATTEMPT,
        CYCLES_OF_BOUNDED_WAITS,
        SYNCHRONOUS_TICKS,
    ],
)
def test_deterministic_tick_loops_pass(body):
    assert not _tick_poll_hits(body)


LOCKED_READ = """\
def claim(cache, spec):
    with cache._session(write=True) as session:
        row = session.scalar(select(Op).with_for_update(skip_locked=True))
        {call}
"""


@pytest.mark.parametrize(
    "call",
    [
        "cache._object_is_stored(spec)",
        "cache._transfer_need(manifest, transfer, replaces=False)",
        "cache._transfer_state_for_manifest(manifest, force=False)",
        "cache._running_transfers(session)",
        "cache._verified_bytes(session, digest)",
        "os.scandir(root)",
        "path.lstat()",
    ],
)
def test_storage_io_inside_a_locking_transaction_fails(call):
    source = LOCKED_READ.format(call=call)
    lines = {1, 2, 3, 4}
    assert check_source(PATH, source, lines, modes=("locked-io",))
    assert not check_source(PATH, source, set(), modes=("locked-io",))


@pytest.mark.parametrize(
    "call",
    [
        "cache._transfer_state_for_manifest(manifest, force=False, cached=stored)",
        "cache._payload_or_none(row)",
    ],
)
def test_facts_read_before_the_transaction_pass(call):
    source = LOCKED_READ.format(call=call)
    assert not check_source(PATH, source, {1, 2, 3, 4}, modes=("locked-io",))
    unlocked = source.replace(".with_for_update(skip_locked=True)", "")
    assert not check_source(
        PATH,
        unlocked.replace(call, "cache._object_is_stored(spec)"),
        {1, 2, 3, 4},
        modes=("locked-io",),
    )


_TITLE_IDENTITY = (
    "alias = assignment.recipe_title.lower().replace(' ', '-')\n"
    "slug = slugify(recipe.title)\n"
    "key = item.identity.title.casefold()\n"
    "shown = recipe.title.title()\n"
    "label = f'{assignment.recipe_title} {version}'\n"
)


def test_a_name_built_from_a_display_title_is_flagged() -> None:
    path = "control/src/vonk_control/fleet_profiles/example.py"
    for line in (1, 2, 3):
        assert check_source(path, _TITLE_IDENTITY, {line}, modes=("identity",)), line
    assert not check_source(path, _TITLE_IDENTITY, {4, 5}, modes=("identity",))


def test_a_title_search_filter_is_allowed() -> None:
    path = "control/src/vonk_control/library_projection/views.py"
    source = "ok = wanted_search in item.identity.title.casefold()\n"
    assert not check_source(path, source, {1}, modes=("identity",))
    assert check_source(
        path, "x = item.identity.title.casefold()\n", {1}, modes=("identity",)
    )


def test_model_file_removal_belongs_to_the_removal_executor():
    source = "def go(cache):\n    cache._remove_model_object_files(digest)\n"
    other = "control/src/vonk_control/model_cache/other.py"
    executor = "control/src/vonk_control/model_cache/removal_execution.py"
    assert check_source(other, source, {2}, modes=("model-sets",))
    assert not check_source(executor, source, {2}, modes=("model-sets",))
    assert not check_source(other, source, {1}, modes=("model-sets",))


_SCRATCH_PATH = "control/src/vonk_control/recipe_packages/__init__.py"


@pytest.mark.parametrize(
    "source",
    [
        'temporary = target.with_suffix(".tmp")\n',
        'partial = root / f".{RELEASE_LIBRARY}.part"\n',
        'temporary = report.with_name(f".{report.name}.part")\n',
        'candidate = root / "snapshot.candidate.tmp"\n',
        'partial = target.parent / (target.name + ".part")\n',
    ],
)
def test_a_shared_scratch_name_is_flagged(source: str) -> None:
    assert check_source(_SCRATCH_PATH, source, {1}, modes=("scratch-names",))
    assert not check_source(_SCRATCH_PATH, source, set(), modes=("scratch-names",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (
            _SCRATCH_PATH,
            (
                "fd, name = tempfile.mkstemp(\n"
                '    prefix=".snapshot.", suffix=".tmp", dir=root\n'
                ")\n"
            ),
        ),
        (
            _SCRATCH_PATH,
            'temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")\n',
        ),
        (_SCRATCH_PATH, 'orphans = root.glob(".snapshot.*.tmp")\n'),
        (_SCRATCH_PATH, 'staging = root / ".tmp"\n'),
        (
            "control/src/vonk_control/model_cache/storage.py",
            'return root / "partials" / set_digest / f"{digest}.part"\n',
        ),
        ("control/tests/test_x.py", 'path = tmp_path / "model.part"\n'),
    ],
)
def test_private_or_classified_scratch_names_pass(path: str, source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(path, source, lines, modes=("scratch-names",))


_SAVEPOINT_PATH = "control/src/vonk_control/admission_locking.py"


@pytest.mark.parametrize(
    "source",
    [
        # with sessions.begin() exits while this savepoint is still open:
        # before_commit sees only the savepoint and the staged events drop.
        "savepoint = session.begin_nested()\n",
        "session.begin_nested()\n",
        "with lock(), session.connection().begin_nested().begin():\n    pass\n",
    ],
)
def test_a_savepoint_outside_a_with_block_is_flagged(source: str) -> None:
    assert check_source(_SAVEPOINT_PATH, source, {1}, modes=("savepoints",))
    assert not check_source(_SAVEPOINT_PATH, source, set(), modes=("savepoints",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (_SAVEPOINT_PATH, "with session.begin_nested():\n    pass\n"),
        (
            _SAVEPOINT_PATH,
            "with admitting(), connection.begin_nested() as savepoint:\n    pass\n",
        ),
        ("control/tests/test_x.py", "savepoint = session.begin_nested()\n"),
    ],
)
def test_a_savepoint_in_a_with_block_passes(path: str, source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(path, source, lines, modes=("savepoints",))


_BARRIER_TEST = "control/tests/test_example.py"


@pytest.mark.parametrize(
    "source",
    [
        # A patched method the product calls twice strands its thread here.
        "import threading\nbarrier = threading.Barrier(2, timeout=30)\n",
        "from threading import Barrier\nboth = Barrier(2)\n",
        "from threading import Barrier as Gate\nboth = Gate(2)\n",
        "import threading as t\nboth = t.Barrier(2)\n",
    ],
)
def test_a_raw_barrier_in_a_control_test_is_flagged(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_BARRIER_TEST, source, lines, modes=("barriers",))
    # Only an added line can fail the change.
    assert not check_source(_BARRIER_TEST, source, {1}, modes=("barriers",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (_BARRIER_TEST, "both = Rendezvous(2)\n"),
        (_BARRIER_TEST, "import asyncio\nbarrier = asyncio.Barrier(2)\n"),
        # The rendezvous itself is built on the one raw barrier.
        (
            "control/tests/insert_collision_support.py",
            "import threading\nbarrier = threading.Barrier(2)\n",
        ),
        (
            "control/src/vonk_control/x.py",
            "import threading\nb = threading.Barrier(2)\n",
        ),
    ],
)
def test_a_rendezvous_or_an_out_of_scope_barrier_passes(path: str, source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(path, source, lines, modes=("barriers",))


_ALIAS_PATH = "control/src/vonk_control/recipe_routes/candidate.py"


@pytest.mark.parametrize(
    "source",
    [
        "_RULE = re.compile(r'[a-z]{0,62}')\nok = _RULE.fullmatch(run.alias)\n",
        "ok = re.fullmatch(_IDENTIFIER, alias)\n",
        "if len(alias) > 63:\n    pass\n",
        "def f(alias: str | None = Query(pattern=_PATTERN, max_length=63)): ...\n",
        "def f(alias: Annotated[str, Field(max_length=LIMIT)]): ...\n",
    ],
)
def test_an_alias_checked_with_its_own_rule_is_flagged(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_ALIAS_PATH, source, lines, modes=("alias-rules",))
    assert check_source(
        "src/cluster_profiles/x.py", source, lines, modes=("alias-rules",)
    )
    assert not check_source(_ALIAS_PATH, source, set(), modes=("alias-rules",))


@pytest.mark.parametrize(
    "source",
    [
        "ok = re.fullmatch(ENDPOINT_ALIAS_PATTERN, run.alias)\n",
        "_ALIAS = re.compile(ENDPOINT_ALIAS_PATTERN)\nok = _ALIAS.fullmatch(alias)\n",
        "unique = len(aliases) == len(set(aliases))\n",
        "def f(alias: EndpointAlias | None = Query(default=None)): ...\n",
        "def f(alias=Query(pattern=ENDPOINT_ALIAS_PATTERN)): ...\n",
        "ok = _UPSTREAM.fullmatch(model)\n",
    ],
)
def test_an_alias_checked_with_the_contract_passes(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(_ALIAS_PATH, source, lines, modes=("alias-rules",))


def test_demand_exceeds_capacity_is_emitted_only_at_its_classified_sites():
    for probe in (
        "code = ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY\n",
        "code = RunSwitchCode.RESOURCE_DEMAND_EXCEEDS_CAPACITY\n",
    ):
        # Probe: an emission on the memory path fails the rule.
        for path in (
            "control/src/vonk_control/resource_planning/capacity.py",
            "control/src/vonk_control/run_admission.py",
            "control/src/vonk_control/run_switch_operations/resource_fit.py",
        ):
            assert check_source(path, probe, {1}, modes=("capacity-refusals",))
        assert not check_source(
            "control/src/vonk_control/fleet_profiles/colocation.py",
            probe,
            {1},
            modes=("capacity-refusals",),
        )
        assert not check_source(
            "control/src/vonk_control/resource_planning/capacity.py",
            probe,
            {2},
            modes=("capacity-refusals",),
        )
    # The disk site carries its own marker; the marker is per line.
    disk = "code = RunSwitchCode.RESOURCE_DEMAND_EXCEEDS_CAPACITY  # capacity-refusal: disk\n"
    assert not check_source(
        "control/src/vonk_control/run_switch_operations/resource_fit.py",
        disk,
        {1},
        modes=("capacity-refusals",),
    )


@pytest.mark.parametrize(
    "probe",
    [
        "raise HTTPException(status_code=422, detail='time is in the future')\n",
        "raise HTTPException(\n    status_code=422,\n    detail='x',\n)\n",
        "return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)\n",
    ],
)
def test_new_agent_facing_422_is_flagged(probe: str) -> None:
    # Wrong implementation: a clock or race answered 422 makes the Spark drop
    # bytes that would be accepted a moment later.
    path = "control/src/vonk_control/agent_api/observations.py"
    line = next(
        number for number, text in enumerate(probe.splitlines(), 1) if "422" in text
    )
    assert check_source(path, probe, {line}, modes=("agent-422",))


def test_agent_422_guard_flags_positional_and_split_422() -> None:
    path = "control/src/vonk_control/agent_api/observations.py"
    assert check_source(
        path, "raise HTTPException(422, 'x')\n", {1}, modes=("agent-422",)
    )
    split = "raise HTTPException(\n    422,\n    'x',\n)\n"
    assert check_source(path, split, {1, 2}, modes=("agent-422",))


def test_agent_422_guard_flags_any_handler_that_answers_422() -> None:
    # Wrong implementation: any caught exception, however far down the handler
    # body, answers 422 and the Spark discards the evidence.
    path = "control/src/vonk_control/agent_api/observations.py"
    probe = (
        "try:\n"
        "    work()\n"
        "except ValueError as error:\n"
        "    log(error)\n"
        "    log(error)\n"
        "    log(error)\n"
        "    log(error)\n"
        "    raise HTTPException(status_code=422, detail=str(error)) from None\n"
    )
    hits = check_source(path, probe, {3}, modes=("agent-422",))
    assert any("except handler" in hit for hit in map(str, hits))
    routed = (
        "try:\n    work()\n"
        "except ValueError as error:\n    contract_verdict_or_wait(error)\n"
    )
    assert not check_source(path, routed, {3}, modes=("agent-422",))


def test_agent_422_guard_allows_marked_verdicts_and_other_places() -> None:
    marked = "raise HTTPException(status_code=422)  # 422-verdict: body validation\n"
    path = "control/src/vonk_control/agent_api/observations.py"
    assert not check_source(path, marked, {1}, modes=("agent-422",))
    other = "raise HTTPException(status_code=422)\n"
    assert not check_source(
        "control/src/vonk_control/library_api.py", other, {1}, modes=("agent-422",)
    )
    assert not check_source(path, other, set(), modes=("agent-422",))


@pytest.mark.parametrize(
    "handler",
    [
        (
            "except (EnrollmentDenied, ValueError) as error:\n"
            "    raise HTTPException(status_code=403, detail=str(error)) from None\n"
        ),
        (
            "except (\n    EnrollmentDenied,\n    ValueError,\n) as error:\n"
            "    raise SecurityHTTPError(status_code=401, detail='x')\n"
        ),
        "except OSError:\n    raise HTTPException(403, 'x')\n",
    ],
)
def test_broad_exception_may_not_answer_401_or_403(handler: str) -> None:
    # Wrong implementation: an internal ValueError answered 403 and the Spark's
    # agent exited (fatal 401/403) instead of retrying.
    path = "control/src/vonk_control/agent_api/authority.py"
    assert check_source(path, "try:\n    work()\n" + handler, {3}, modes=("agent-422",))


def test_comment_after_except_header_does_not_hide_the_handler() -> None:
    path = "control/src/vonk_control/agent_api/authority.py"
    source = (
        "try:\n    work()\n"
        "except ValueError:  # internal\n"
        "    raise HTTPException(status_code=403, detail='x')\n"
    )
    assert check_source(path, source, {3}, modes=("agent-422",))


def test_a_403_added_into_an_existing_broad_handler_is_flagged_on_its_own_line() -> (
    None
):
    path = "control/src/vonk_control/agent_api/authority.py"
    source = (
        "try:\n    work()\n"
        "except (KeyError, TypeError, ValueError):\n"
        "    raise HTTPException(status_code=403, detail='x')\n"
    )
    assert check_source(path, source, {4}, modes=("agent-422",))
    assert not check_source(path, source, {1}, modes=("agent-422",))


def test_typed_security_refusal_may_answer_403() -> None:
    path = "control/src/vonk_control/agent_api/authority.py"
    typed = (
        "try:\n    work()\n"
        "except EnrollmentDenied as error:\n"
        "    raise HTTPException(status_code=403, detail=str(error)) from None\n"
    )
    assert not check_source(path, typed, {3}, modes=("agent-422",))
    routed = (
        "try:\n    work()\n"
        "except (EnrollmentDenied, ValueError) as error:\n"
        "    contract_verdict_or_wait(error)\n"
    )
    assert not check_source(path, routed, {3}, modes=("agent-422",))


@pytest.mark.parametrize(
    "source,line",
    [
        ("def review(s, now):\n    return _Evidence.read(s, now)\n", 2),
        ("def review(s, now):\n    return _CollectorEvidence.read(s, now)\n", 2),
        ("def review(s, now):\n    return _Evidence.read(s, now, recipes=None)\n", 2),
        ("def review(s):\n    return profiles_naming(s)\n", 2),
        ("def review(s):\n    return profiles_naming(s, None)\n", 2),
        ("def review(s, now):\n    return live_tokens(s, now)\n", 2),
        ("def review(s):\n    return operation_tokens(s, recent=True)\n", 2),
        ("def review(s, n, now):\n    return spark_eviction_capacity(s, n, now)\n", 2),
        ("def review(c, s, n):\n    return c.relief_for_spark(s, n)\n", 2),
        ("def review(s):\n    return pinned_by_heads(s)\n", 2),
        ("def review(s):\n    return _profile_pointers(s)\n", 2),
        ("def review(s, a):\n    return runtime_image_reference_findings(s, a)\n", 2),
        ("def review(s, a):\n    return model_set_reference_reasons(s, a)\n", 2),
    ],
)
def test_a_request_path_may_not_read_the_whole_collection(source, line):
    assert check_source(PATH, source, {line}, modes=("collection-readers",))


@pytest.mark.parametrize(
    "path,source",
    [
        # A collector decides over every item; that is its job.
        (
            "control/src/vonk_control/unused_storage_collection/removal.py",
            "def run(s, now):\n    return _CollectorEvidence.read(s, now)\n",
        ),
        (
            "control/src/vonk_control/catalog_revision_collection.py",
            "def run(s, now):\n    return live_tokens(s, now)\n",
        ),
        (
            "control/src/vonk_control/terminal_history_collection.py",
            "def run(s):\n    return live_tokens(s, 1)\n",
        ),
        # Target-keyed reads are what a request path does.
        (PATH, "def review(s, a):\n    return unused_for(s, a)\n"),
        (PATH, "def review(s):\n    return operation_tokens(s, recent=False)\n"),
        (PATH, "def review(s, r):\n    return profiles_naming(s, r)\n"),
        (
            PATH,
            "def review(s, now, r):\n    return _Evidence.read(s, now, recipes=r)\n",
        ),
        ("control/tests/test_x.py", "def t(s):\n    return live_tokens(s, 1)\n"),
    ],
)
def test_collectors_and_target_keyed_reads_pass_the_collection_guard(path, source):
    assert not check_source(path, source, {2, 3, 4}, modes=("collection-readers",))


def test_a_whole_collection_read_already_there_does_not_fail_an_unrelated_change():
    source = "def review(s, now):\n    return _Evidence.read(s, now)\n\nvalue = 1\n"
    assert not check_source(PATH, source, {4}, modes=("collection-readers",))


@pytest.mark.parametrize(
    "source",
    [
        "bad = row.content_digest != document_sha256(row.document)\n",
        "bad = _digest(row.plan) != row.plan_digest\n",
        "bad = intended.reviewed_plan_digest != _digest(plan.reviewed)\n",
        "bad = row.profile_digest != _digest(_profile_document(profile))\n",
        "bad = sha256(canonical_message(row.payload)).hexdigest() != row.request_sha256\n",
    ],
)
def test_any_stored_digest_column_compared_with_a_fresh_hash_is_a_rehash(source):
    assert f"{PATH}:1: stored row re-hashed" in check_source(
        PATH, source, {1}, modes=("identity",)
    )


@pytest.mark.parametrize(
    "path,source",
    [
        # Where bytes arrive, the digest is checked against them.
        (
            "control/src/vonk_control/model_cache/download.py",
            "bad = row.content_digest != document_sha256(row.document)\n",
        ),
        (
            "control/src/vonk_control/inventory_repository.py",
            "bad = report.payload_digest != _digest(report.payload)\n",
        ),
        # Two recorded digests, or a digest of something that is not stored.
        (PATH, "same = a.content_digest == b.content_digest\n"),
        (PATH, "bad = row.content_digest != _digest(row.name)\n"),
        (PATH, "bad = row.size != len(row.document)\n"),
    ],
)
def test_ingress_and_unrelated_comparisons_are_not_rehashes(path, source):
    assert not [
        hit
        for hit in check_source(path, source, {1}, modes=("identity",))
        if "re-hashed" in hit
    ]


@pytest.mark.parametrize(
    "source,lines",
    [
        (
            (
                "def f(job):\n"
                "    a = read_recipe(job.document)\n"
                "    b = read_recipe(job.document)\n"
            ),
            {2, 3},
        ),
        (
            (
                "def f(i):\n"
                "    a = parse_stored_installation_plan(i.plan)\n"
                "    return parse_stored_installation_plan(i.plan).nodes\n"
            ),
            {2, 3},
        ),
        (
            (
                "def f(j):\n"
                "    a = read_row_column(j, 'payload')\n"
                "    b = read_row_column(j, 'payload')\n"
            ),
            {2, 3},
        ),
        (
            "def f(j):\n    return parse_stored_job_plan(read_row_column(j, 'plan'))\n",
            {2},
        ),
        (
            (
                "def f(rows):\n"
                "    for r in rows:\n"
                "        a = read_model(r.document)\n"
                "        b = read_model(r.document)\n"
            ),
            {3, 4},
        ),
    ],
)
def test_one_stored_value_parsed_twice_in_a_function_is_flagged(source, lines):
    hits = check_source(PATH, source, lines, modes=("repeated-parse",))
    assert hits
    assert all("parsed twice" in hit for hit in hits)


@pytest.mark.parametrize(
    "source",
    [
        # Different values.
        "def f(a, b):\n    return read_recipe(a.document), read_recipe(b.document)\n",
        # Different parsers, or different columns.
        "def f(j):\n    return read_recipe(j.document), read_model(j.document)\n",
        "def f(j):\n    return read_row_column(j, 'plan'), read_row_column(j, 'payload')\n",
        # One of two branches runs.
        (
            "def f(j, x):\n"
            "    if x:\n"
            "        return read_recipe(j.document)\n"
            "    else:\n"
            "        return read_recipe(j.document)\n"
        ),
        # A new row on every iteration, and a function of its own.
        (
            "def f(rows, more):\n"
            "    for r in rows:\n"
            "        read_recipe(r.document)\n"
            "    for r in more:\n"
            "        read_recipe(r.document)\n"
        ),
        (
            "def f(j):\n"
            "    def inner():\n"
            "        return read_recipe(j.document)\n"
            "    return read_recipe(j.document)\n"
        ),
        "def f(j):\n    return parse_stored_job_plan(j.plan)\n",
    ],
)
def test_distinct_or_exclusive_parses_pass_the_repeat_guard(source):
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(PATH, source, lines, modes=("repeated-parse",))


def test_an_old_double_parse_does_not_fail_an_unrelated_change():
    source = (
        "def f(j):\n    a = read_recipe(j.document)\n    b = read_recipe(j.document)\n\n"
        "value = 1\n"
    )
    assert not check_source(PATH, source, {5}, modes=("repeated-parse",))


@pytest.mark.parametrize(
    "source,line",
    [
        ("value = accepts_legacy(row)\n", 1),
        ("value = shim(row)\n", 1),
        ("value = 'backwards compatibility'\n", 1),
        ("backfill(column)\n", 1),
        ("value = 'an older agent sends this'\n", 1),
        ("value = adopt_legacy(row)\n", 1),
        ("x: Annotated[str, BeforeValidator(state_adopter(S))]\n", 1),
        ("x = StateAlias.CANCELLING\n", 1),
        ("def f(row):\n    retire_as_unknown('s', row.id, R, 'why')\n", 2),
        (
            (
                "def f(rows):\n"
                "    for row in rows:\n"
                "        try:\n"
                "            parse(row)\n"
                "        except ValueError:\n"
                "            continue\n"
            ),
            5,
        ),
        (
            (
                "def f(row):\n"
                "    try:\n"
                "        parse(row)\n"
                "    except (ValidationError, TypeError):\n"
                "        pass\n"
            ),
            4,
        ),
    ],
)
def test_new_legacy_or_damage_tolerance_is_flagged(source, line):
    assert check_source(PATH, source, {line}, modes=("legacy",))
    protocol = "agent_protocol/src/vonk_agent_protocol/example.py"
    assert check_source(protocol, source, {line}, modes=("legacy",))


@pytest.mark.parametrize(
    "source",
    [
        "value = 'compatible'\n",
        "return_value = 1\n",
        "return retire_as_unknown('s', row.id, R, 'why')\n",
        (
            "try:\n    parse(row)\nexcept ValueError as error:\n"
            "    raise Refused() from error\n"
        ),
        "try:\n    parse(row)\nexcept OSError:\n    pass\n",
    ],
)
def test_current_contract_code_passes_the_legacy_guard(source):
    if source.startswith("return"):
        source = f"def f(row):\n    {source}"
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(PATH, source, lines, modes=("legacy",))


@pytest.mark.parametrize(
    "path",
    [
        "control/src/vonk_control/lifecycle/evidence.py",
        "control/src/vonk_control/stored_json.py",
        "control/src/vonk_control/strict_json.py",
    ],
)
def test_the_stored_json_readers_are_not_exempt_from_the_legacy_guard(path):
    assert check_source(path, "value = 'legacy'\n", {1}, modes=("legacy",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("control/web/src/pages/x.tsx", 'if (job.state === "partial") {\n'),
        ("control/web/src/pages/x.tsx", 'const old = "cancelling" !== detail.state;\n'),
        ("control/web/src/pages/x.ts", 'if (row.state?.value == "compensated") {}\n'),
        ("control/web/src/pages/x.tsx", "const wait = LEGACY_WAIT_STATE;\n"),
        ("control/web/src/styles.css", ".row-waiting-for-operator{color:red}\n"),
        ("src/cluster_profiles/x.py", 'WAIT = "waiting-for-operator"\n'),
        ("scripts/x.py", "from x import STATE_INPUT_ALIASES\n"),
    ],
)
def test_a_retired_state_spelling_is_flagged_in_every_language(path, source):
    assert check_source(path, source, {1}, modes=("legacy",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("control/web/src/pages/x.tsx", 'if (job.preparation === "draft") {\n'),
        ("control/web/src/pages/x.tsx", 'if (coverage === "partial") {\n'),
        ("control/web/src/pages/x.tsx", "// the old waiting-for-operator word\n"),
        ("control/web/src/api/vocabulary.generated.ts", "const legacy = 1;\n"),
        ("control/web/src/pages/x.test.tsx", 'expect(state === "partial");\n'),
    ],
)
def test_current_web_code_passes_the_legacy_guard(path, source):
    assert not check_source(path, source, {1}, modes=("legacy",))


@pytest.mark.parametrize(
    "path", ["control/tests/test_x.py", "rust/crates/x/src/lib.rs"]
)
def test_tests_and_other_languages_are_outside_the_legacy_guard(path):
    assert not check_source(path, "value = 'legacy'\n", {1}, modes=("legacy",))


@pytest.mark.parametrize(
    "path,source",
    [
        # A request entry point in the collector directory is a request path.
        (
            "control/src/vonk_control/unused_storage_collection/common.py",
            "def capacity(s, now):\n    return _Evidence.read(s, now)\n",
        ),
        (
            "control/src/vonk_control/unused_storage_collection/service.py",
            (
                "class C:\n    def relief_for_spark(self, s, now):\n"
                "        return _Evidence.read(s, now)\n"
            ),
        ),
        (
            "control/src/vonk_control/unused_storage_collection/__init__.py",
            "def review(s, n, now):\n    return spark_eviction_capacity(s, n, now)\n",
        ),
    ],
)
def test_the_collector_directory_is_not_exempt_as_a_whole(path, source):
    assert check_source(path, source, {2, 3}, modes=("collection-readers",))


@pytest.mark.parametrize(
    "path,source",
    [
        (
            "control/src/vonk_control/unused_storage_collection/service.py",
            (
                "class C:\n    def collect(self, s, now):\n"
                "        return _CollectorEvidence.read(s, now)\n"
            ),
        ),
        (
            "control/src/vonk_control/unused_storage_collection/evidence.py",
            "def read(s, now):\n    return operation_tokens(s, now)\n",
        ),
    ],
)
def test_the_collector_passes_may_read_the_whole_collection(path, source):
    assert not check_source(path, source, {2, 3}, modes=("collection-readers",))


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            (
                "def f(row):\n    fresh = document_sha256(row.document)\n"
                "    return fresh != row.content_digest\n"
            ),
            3,
        ),
    ],
)
def test_a_two_step_rehash_is_flagged(source, expected):
    assert f"{PATH}:{expected}: stored row re-hashed" in check_source(
        PATH, source, {expected}, modes=("identity",)
    )


@pytest.mark.parametrize(
    "path",
    [
        "control/src/vonk_control/catalog_sync_contract.py",
        "control/src/vonk_control/telemetry_maintenance.py",
        "control/src/vonk_control/model_cache/download_admission.py",
    ],
)
def test_ingress_prefixes_do_not_over_match(path):
    source = "bad = row.content_digest != document_sha256(row.document)\n"
    assert f"{path}:1: stored row re-hashed" in check_source(
        path, source, {1}, modes=("identity",)
    )


@pytest.mark.parametrize(
    "source,line",
    [
        ("value = legacy_claim(row)\n", 1),
        ("value = 'backward compat'\n", 1),
        ("value = _ = retire_as_unknown('s', row.id, R, 'why')\n", 1),
        ("def f(row):\n    _ = retire_as_unknown('s', row.id, R, 'why')\n", 2),
        (
            "def f(row):\n    unused = retire_as_unknown('s', row.id, R, 'why')\n",
            2,
        ),
        ("try:\n    work()\nexcept Exception:\n    return None\n", 3),
        ("try:\n    work()\nexcept:\n    pass\n", 3),
        ("try:\n    work()\nexcept BaseException:\n    ...\n", 3),
        ("try:\n    work()\nexcept ValueError:\n    continue\n", 3),
        ("with suppress(ValueError):\n    work()\n", 1),
        ("with contextlib.suppress(Exception):\n    work()\n", 1),
    ],
)
def test_more_tolerance_shapes_are_flagged(source, line):
    assert check_source(PATH, source, {line}, modes=("legacy",))


@pytest.mark.parametrize(
    "source",
    [
        "value = settings.compatibility\n",
        "value = row.compatibility\n",
        "def f(row):\n    answer = retire_as_unknown('s', row.id, R, 'why')\n    return answer\n",
        "try:\n    work()\nexcept OSError:\n    pass\n",
        "try:\n    work()\nexcept Exception:\n    log(1)\n    raise\n",
        "with suppress(FileNotFoundError):\n    work()\n",
    ],
)
def test_more_current_code_passes_the_legacy_guard(source):
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(PATH, source, lines, modes=("legacy",))


def test_a_condition_and_its_body_both_run():
    source = (
        "def f(j):\n"
        "    if read_recipe(j.document):\n"
        "        return read_recipe(j.document)\n"
    )
    assert check_source(PATH, source, {2, 3}, modes=("repeated-parse",))


def test_a_keyword_argument_is_part_of_the_value_parsed():
    source = (
        "def f(j):\n"
        "    a = read_row_column(j, 'plan', strict=True)\n"
        "    b = read_row_column(j, 'plan', strict=False)\n"
    )
    assert not check_source(PATH, source, {2, 3}, modes=("repeated-parse",))


@pytest.mark.parametrize(
    "source",
    [
        "# keep working for legacy rows\nvalue = 1\n",
        "value = 1  # backfill the column\n",
        'def f():\n    """Reads legacy rows, a compat shim."""\n    return 1\n',
        'class C:\n    """An older agent."""\n',
        '"""Module about legacy data."""\nvalue = 1\n',
        "cursor.execute('PRAGMA legacy_alter_table = ON')\n",
    ],
)
def test_comments_docstrings_and_pragmas_are_quiet(source):
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(PATH, source, lines, modes=("legacy",))


@pytest.mark.parametrize(
    "tail",
    [
        "log(error)\n        return None",
        "log(error)\n        return []",
        "log(error)\n        return {}",
        "log(error)\n        return ()",
        "log(error)\n        return ''",
        "log(error)\n        return set()",
        "log(error)\n        value = None",
        "log(error)\n        value = []",
        "log(error)\n        continue",
        "log(error)\n        pass",
    ],
)
def test_a_handler_ending_in_nothing_is_flagged_whatever_precedes_it(tail):
    source = f"def f(row):\n    try:\n        parse(row)\n    except ValueError as error:\n        {tail}\n"
    assert check_source(PATH, source, {4}, modes=("legacy",))


def test_json_decode_errors_and_raising_handlers():
    flagged = "try:\n    work()\nexcept json.JSONDecodeError:\n    pass\n"
    assert check_source(PATH, flagged, {3}, modes=("legacy",))
    kept = "try:\n    work()\nexcept ValueError as e:\n    log(e)\n    raise\n"
    assert not check_source(PATH, kept, {3}, modes=("legacy",))


@pytest.mark.parametrize(
    "path,source",
    [
        (
            "control/src/vonk_control/unused_storage_collection/references.py",
            "def _installation_kept(s):\n    return _Evidence.read(s, 1)\n",
        ),
        (
            "control/src/vonk_control/unused_storage_collection/evidence.py",
            "def helper(s):\n    return operation_tokens(s, 1)\n",
        ),
    ],
)
def test_collector_modules_are_exempt_per_function(path, source):
    assert check_source(path, source, {2}, modes=("collection-readers",))


def test_the_collector_functions_of_the_evidence_modules_stay_exempt():
    source = "def read(s, now):\n    return operation_tokens(s, now)\n"
    path = "control/src/vonk_control/unused_storage_collection/evidence.py"
    assert not check_source(path, source, {2}, modes=("collection-readers",))


API_PATH = "control/src/vonk_control/example_api.py"
MODEL_CACHE_PATH = "control/src/vonk_control/model_cache/example.py"
AGENT_API_PATH = "control/src/vonk_control/agent_api/example.py"
COLLECTOR_PATH = "control/src/vonk_control/terminal_history_collection.py"
GROWING = [
    "Job",
    "JobAttempt",
    "FleetProfile",
    "InstallationNode",
    "NodeTelemetrySample",
]


@pytest.mark.parametrize("table", GROWING)
def test_the_request_scan_guard_covers_the_tables_that_grow_with_the_fleet(table):
    """One table set: the tables the old collection rule named are the scan rule's."""
    unnarrowed = f"def rows(s):\n    return s.scalars(select({table}))\n"
    column = f"def rows(s):\n    return s.execute(select({table}.id))\n"
    keyed = f"def rows(s, i):\n    return s.scalars(select({table}).where({table}.id == i))\n"
    paged = f"def rows(s):\n    return s.scalars(select({table}).limit(10))\n"
    assert check_source(API_PATH, unnarrowed, {2}, modes=("request-scans",))
    assert check_source(API_PATH, column, {2}, modes=("request-scans",))
    assert not check_source(API_PATH, keyed, {2}, modes=("request-scans",))
    assert not check_source(API_PATH, paged, {2}, modes=("request-scans",))


@pytest.mark.parametrize("path", [MODEL_CACHE_PATH, AGENT_API_PATH, PATH])
@pytest.mark.parametrize("table", GROWING)
def test_selecting_every_row_of_a_growing_table_is_flagged_outside_api_modules(
    path, table
):
    """The audit sites (model_cache, agent_api, worker ticks) are not API-scoped."""
    unnarrowed = f"def rows(s):\n    return s.scalars(select({table}))\n"
    column = f"def rows(s):\n    return s.execute(select({table}.id))\n"
    assert check_source(path, unnarrowed, {2}, modes=("collection-readers",))
    assert check_source(path, column, {2}, modes=("collection-readers",))


@pytest.mark.parametrize(
    "path,source",
    [
        (
            MODEL_CACHE_PATH,
            "def rows(s):\n    return s.scalars(select(Job).where(Job.state.in_(LIVE)))\n",
        ),
        (
            MODEL_CACHE_PATH,
            "def rows(s):\n    return s.scalars(select(Job).limit(10))\n",
        ),
        (
            AGENT_API_PATH,
            "def rows(s, n):\n    q = select(InstallationNode)\n    return s.scalars(q.where(InstallationNode.node_id == n))\n",
        ),
        (AGENT_API_PATH, "def rows(s):\n    return s.scalars(select(AgentNode))\n"),
        # A collector decides over every row; that is its job.
        (COLLECTOR_PATH, "def run(s):\n    return s.scalars(select(Job))\n"),
    ],
)
def test_narrowed_other_table_and_collector_selects_pass(path, source):
    assert not check_source(path, source, {2, 3, 4}, modes=("collection-readers",))


CATALOG_PATH = "control/src/vonk_control/library_projection/catalog.py"
_KEYED_SEMI_JOIN = (
    "def rows(s, d):\n"
    "    return s.scalars(\n"
    "        select(CatalogDocumentRevision).where(\n"
    "            CatalogDocumentRevision.slug == d,\n"
    "            CatalogDocumentRevision.content_digest.in_(\n"
    "                select(ModelCacheSet.model_content_sha256)\n"
    "            ),\n"
    "        )\n"
    "    )\n"
)


def test_a_select_inside_a_keyed_outer_query_is_judged_with_the_outer_query():
    """The target-keyed semi-join principle 17 prescribes is not a whole read."""
    inner = {6}
    assert not check_source(
        PATH, _KEYED_SEMI_JOIN, inner, modes=("collection-readers",)
    )
    # A where whose only condition is the subquery is a semi-join with the whole
    # table: the subquery fires (line 3); the keyed outer query is not its scope.
    bare = (
        "def rows(s):\n"
        "    return s.scalars(\n"
        "        select(ModelCacheSet).where(ModelCacheSet.digest.in_(select(Job.digest)))\n"
        "    )\n"
    )
    assert check_source(PATH, bare, {3}, modes=("collection-readers",))
    anded = (
        "def rows(s, d):\n"
        "    return s.scalars(\n"
        "        select(A.x).where(and_(A.k == d, A.y.in_(select(Job.id))))\n"
        "    )\n"
    )
    assert not check_source(PATH, anded, {3}, modes=("collection-readers",))
    unnarrowed = (
        "def rows(s):\n    return s.scalars(select(A.x).join(select(Job.id)))\n"
    )
    assert check_source(PATH, unnarrowed, {2}, modes=("collection-readers",))


@pytest.mark.parametrize(
    "source",
    [
        # A narrowed select elsewhere in the same call is not this select's scope.
        "def f(s, x):\n    return merge(s.scalars(select(Job)).all(), s.scalars(select(RecipeRun).where(RecipeRun.id == x)).all())\n",
        "def f(s, x):\n    return s.execute(select(Job.id).union(select(RecipeRun.id).where(RecipeRun.id == x)))\n",
    ],
)
@pytest.mark.parametrize(
    "mode,path",
    [
        ("collection-readers", MODEL_CACHE_PATH),
        ("request-scans", "control/src/vonk_control/api/example.py"),
    ],
)
def test_a_narrowed_neighbour_does_not_scope_an_unnarrowed_select(source, mode, path):
    assert check_source(path, source, {2}, modes=(mode,))


@pytest.mark.parametrize("mode", ["collection-readers", "request-scans"])
def test_the_startup_index_build_is_exempt_from_both_select_rules(mode):
    source = "def refresh_catalog(s):\n    return s.scalars(select(CatalogDocumentRevision))\n"
    assert not check_source(CATALOG_PATH, source, {2}, modes=(mode,))
    other = source.replace("refresh_catalog", "view")
    assert check_source(CATALOG_PATH, other, {2}, modes=(mode,))


@pytest.mark.parametrize("mode", ["collection-readers", "request-scans"])
def test_limit_none_is_unbounded_for_both_select_rules(mode):
    path = CATALOG_PATH
    none = "def view(s):\n    return s.scalars(select(Job).limit(None))\n"
    bounded = "def view(s):\n    return s.scalars(select(Job).limit(5))\n"
    assert check_source(path, none, {2}, modes=(mode,))
    assert not check_source(path, bounded, {2}, modes=(mode,))


_SELECT_PATHS = [
    ("collection-readers", MODEL_CACHE_PATH),
    ("request-scans", "control/src/vonk_control/api/example.py"),
]
_SELECT_LINES = set(range(2, 12))
# Everything the reviews proved a whole-table select can hide behind. The rule
# errs toward false positives: only a narrowing call chained on the select, or a
# keyed semi-join, gives credit.
_MUST_FIRE = [
    "return merge(s.scalars(select(Job)).all(), s.scalars(select(RecipeRun).where(RecipeRun.id == x)).all())",
    "return s.execute(select(Job.id).union(select(RecipeRun.id).where(RecipeRun.id == x)))",
    "return s.scalars(select(ModelCacheSet).where(ModelCacheSet.digest.in_(select(Job.digest))))",
    "return s.scalars(select(A.x).where(A.y.in_(select(Job.id))))",
    "return s.scalars(select(Job)) + s.scalars(select(RecipeRun).where(RecipeRun.id == x))",
    "return s.scalars(select(Job)) or s.scalars(select(RecipeRun).where(RecipeRun.id == x))",
    "return set(s.scalars(select(Job))) | set(s.scalars(select(RecipeRun).where(RecipeRun.id == x)))",
    "return s.scalars(select(A.x).where((A.k == d) | A.y.in_(select(Job.id))))",
    "return s.scalars(select(A.x).where(or_(A.k == d, A.y.in_(select(Job.id)))))",
    "q = select(Job)\n    a = s.scalars(q.where(Job.id == x))\n    return a, s.scalars(q)",
    "q = select(Job)\n    return s.scalars(other.q.where(Job.id == x))",
    "return filter(lambda r: r.id == x, s.scalars(select(Job)))",
    "return s.scalars(select(Job).limit(None))",
    "q = select(Job)\n    return s.scalars(q)",
    "q = select(Job)\n    if x:\n        q = q.where(Job.id == x)\n    return s.scalars(q)",
    # A rebind narrows only the loads lexically after it in its own block.
    "for i in x:\n        q = select(Job)\n        if i:\n            break\n        q = q.where(Job.id == i)\n    return s.scalars(q)",
    "try:\n        q = select(Job)\n        work()\n        q = q.where(Job.id == x)\n    except E:\n        pass\n    return s.scalars(q)",
    "with suppress(E):\n        q = select(Job)\n        work()\n        q = q.where(Job.id == x)\n    return s.scalars(q)",
]
_MUST_PASS = [
    "return s.scalars(select(Job).where(Job.id == x))",
    "return s.scalars(select(Job).filter(Job.id == x))",
    "return s.scalars(select(Job).limit(5))",
    "q = select(Job)\n    return s.scalars(q.where(Job.id == x))",
    "return s.scalars(select(CDR).where(CDR.slug == d, CDR.content_digest.in_(select(MCS.model_content_sha256))))",
    "return s.scalars(select(CDR).where(and_(CDR.slug == d, CDR.content_digest.in_(select(MCS.model_content_sha256)))))",
    "return s.scalars(select(CDR).where((CDR.slug == d) & CDR.content_digest.in_(select(MCS.model_content_sha256))))",
    "q = select(Job)\n    q = q.where(Job.id == x)\n    return s.scalars(q)",
    "q = select(Job)\n    if x:\n        q = q.where(Job.id == x)\n    else:\n        q = q.where(Job.id == d)\n    return s.scalars(q)",
    "return s.scalars(select(Job).where(Job.id == x, select(MCS.k).where(MCS.k == Job.k).exists()))",
]


@pytest.mark.parametrize("mode,path", _SELECT_PATHS)
@pytest.mark.parametrize("body", _MUST_FIRE)
def test_a_whole_table_select_is_not_rescued_by_anything_but_its_own_narrowing(
    body, mode, path
):
    source = "def f(s, x, d):\n    " + body + "\n"
    assert check_source(path, source, _SELECT_LINES, modes=(mode,)), body


@pytest.mark.parametrize("mode,path", _SELECT_PATHS)
@pytest.mark.parametrize("body", _MUST_PASS)
def test_chained_narrowing_limits_and_the_keyed_semi_join_pass(body, mode, path):
    source = "def f(s, x, d):\n    " + body + "\n"
    assert not check_source(path, source, _SELECT_LINES, modes=(mode,)), body


# EXISTS credits only the select it is chained on: the outer select around it
# is still judged by its own where (no key, no limit). ``collection-readers``
# counts any chained ``where`` as narrowing, so these are request-scans fixtures.
@pytest.mark.parametrize(
    "body",
    [
        "return s.scalars(select(Job).where(select(RunNode.id).exists()))",
        "return s.scalars(select(Job).where(or_(Job.state == 'x', select(RunNode.id).exists())))",
        "return s.scalars(select(Job).where(select(RunNode.id).where(RunNode.job_id == Job.id).exists()))",
        "return s.scalars(select(Job).where(~select(RunNode.id).where(RunNode.job_id == Job.id).exists()))",
    ],
)
def test_an_exists_inside_a_where_does_not_scope_the_outer_select(body):
    source = "def f(s):\n    " + body + "\n"
    path = "control/src/vonk_control/api/example.py"
    assert check_source(path, source, {2}, modes=("request-scans",)), body


_LISTING_PATH = "rust/crates/vonk-agent-helper/src/operations/example.rs"


def _call(callee: str, words: str) -> str:
    return f"fn read(&self) {{\n    let output = self.{callee}(\n        &[{words}],\n        timeout,\n    );\n}}\n"


def _docker_listings(source: str, added: set[int]) -> list[str]:
    return check_source(_LISTING_PATH, source, added, modes=("docker-listings",))


@pytest.mark.parametrize(
    "words",
    [
        '"container", "ls", "--all", "--quiet"',
        '"images", "--no-trunc"',
        '"ps", "--all"',
        '"image", "ls"',
        # The house style: owned strings.
        '"container".to_owned(), "ls".to_owned(), "--quiet".to_owned()',
        '"container".into(), "ls".into(), format!("label=a={x}")',
        '"container", "inspect", "--format", "{{range .Mounts}}{{.Source}}{{end}}", id',
    ],
)
def test_a_captured_docker_listing_or_ranged_inspection_is_flagged(words: str) -> None:
    for callee in ("run_docker", "run_docker_with_timeout", "run_with_timeout"):
        source = _call(callee, words)
        assert _docker_listings(source, {2})
        assert not _docker_listings(source, set())


def test_a_ranged_format_read_through_container_fields_is_flagged() -> None:
    constant = (
        'const OWNERS: &str = "{{.Id}}{{range .Mounts}}{{json .Source}}{{end}}";\n'
        "fn read(&self) {\n"
        "    let output = self.container_fields(id, OWNERS);\n"
        "}\n"
    )
    assert _docker_listings(constant, {3})
    inline = (
        "fn read(&self) {\n"
        '    let output = self.container_fields(&id, "{{range .Mounts}}{{.Source}}{{end}}");\n'
        "}\n"
    )
    assert _docker_listings(inline, {2})
    fixed = inline.replace(
        "{{range .Mounts}}{{.Source}}{{end}}", "{{.State.StartedAt}}"
    )
    assert not _docker_listings(fixed, {2})


def test_an_argument_vector_built_beside_the_call_is_judged() -> None:
    source = (
        "fn read(&self) {\n"
        '    let arguments = vec!["container".to_owned(), "ls".to_owned(), filter];\n'
        "    let output = self.run_docker(&arguments)?;\n"
        "}\n"
    )
    assert _docker_listings(source, {3})
    assert not _docker_listings(source.replace("run_docker", "run_docker_lines"), {3})


def test_each_call_is_judged_on_its_own() -> None:
    source = _call("run_docker_lines", '"container", "ls", "--quiet"') + _call(
        "run_docker", '"container", "ls", "--quiet"'
    ).replace("read", "again")
    assert not _docker_listings(source, {2})
    assert _docker_listings(source, {8})


@pytest.mark.parametrize(
    "source",
    [
        _call("run_docker_lines", '"container", "ls", "--quiet"'),
        _call("run_docker", '"container", "ls", "--quiet", "--last", "5"'),
        _call("run_docker", '"container", "ls", "--filter", format!("id={id}")'),
        _call("run_docker", '"container", "ls", "--filter", format!("name=^/{n}$")'),
        _call("run_docker", '"container", "inspect", "--format", "{{.Id}}", id'),
        _call("run_docker", '"stop", "--timeout", "5", name'),
        _call("run_docker", '"image", "rm", reference'),
    ],
)
def test_a_line_read_exact_target_or_paged_docker_call_passes(source: str) -> None:
    assert not _docker_listings(source, {2})


_STATES_PATH = "control/src/vonk_control/fleet_profiles/example.py"


def _lifecycle_sets(
    source: str, added: set[int], path: str = _STATES_PATH
) -> list[str]:
    return check_source(path, source, added, modes=("lifecycle-sets",))


@pytest.mark.parametrize(
    "spelled",
    [
        # The shape that left a backing-off load out of "live".
        (
            "job_states.words(\n    LifecycleState.QUEUED,\n"
            "    LifecycleState.RUNNING,\n    LifecycleState.NEEDS_OPERATOR,\n)"
        ),
        "{LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}",
        "(State.RUNNING, State.OBSERVING)",
        "job_states.means(row.state, LifecycleState.QUEUED, LifecycleState.BACKOFF)",
        "frozenset({aos.QUEUED, aos.RUNNING})",
        "('queued', 'running')",
        # An "ended" set that counted a wait for a person as ended.
        "{LifecycleState.FAILED, LifecycleState.CANCELLED, LifecycleState.NEEDS_OPERATOR}",
        "job_states.words(LifecycleState.SUCCEEDED, LifecycleState.FAILED)",
        "(LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.FAILED)",
        # Stored words: the same fleet-removal set, string-spelled.
        "child.state in {'failed', 'cancelled', 'needs-operator'}",
        "('succeeded', 'failed', 'cancelled')",
        "('cancelled', 'superseded')",
        # Composed sets.
        (
            "(*job_states.words(LifecycleState.QUEUED),"
            " *job_states.words(LifecycleState.RUNNING))"
        ),
        (
            "job_states.words(LifecycleState.QUEUED)"
            " + job_states.words(LifecycleState.BACKOFF)"
        ),
        # A comparison chain on one subject.
        "state == 'queued' or state == 'running'",
        "state != LifecycleState.SUCCEEDED and state != LifecycleState.CANCELLED",
    ],
)
def test_a_hand_spelled_open_state_set_is_flagged(spelled: str) -> None:
    source = f"LIVE = {spelled}\n"
    lines = set(range(1, source.count("\n") + 1))
    assert _lifecycle_sets(source, lines)
    # Only an added line can fail the change.
    assert not _lifecycle_sets(source, set())


@pytest.mark.parametrize(
    "named",
    [
        "fleet_profile_states.LIVE",
        "job_states.words(LifecycleState.NEEDS_OPERATOR)",
        # Every lifecycle state: the vocabulary itself.
        "tuple(LifecycleState)",
        "(\n    "
        + ",\n    ".join(f"LifecycleState.{state.name}" for state in LifecycleState)
        + ",\n)",
        # Another machine's states.
        "{RunState.STARTING, RunState.RUNNING, RunState.STOPPING}",
        # "failed" alone is an installation's word too.
        "('installed', 'failed')",
        # Two subjects, one state each.
        "state == 'queued' or other == 'running'",
        # A statement around one named predicate.
        "select(Job).where(Job.state.in_(job_states.LIVE), Job.kind == 'x')",
        "(*job_states.LIVE, *job_states.words(LifecycleState.FAILED))",
    ],
)
def test_a_named_predicate_or_vocabulary_passes(named: str) -> None:
    assert not _lifecycle_sets(f"LIVE = {named}\n", {1})


@pytest.mark.parametrize(
    "owner",
    [
        "control/src/vonk_control/fleet_profile_states.py",
        "control/src/vonk_control/model_cache_contract.py",
        "control/src/vonk_control/run_switch_contract/vocabulary.py",
    ],
)
def test_a_states_or_contract_module_owns_its_sets(owner: str) -> None:
    source = "LIVE = words(LifecycleState.QUEUED, LifecycleState.RUNNING)\n"
    assert _lifecycle_sets(source, {1})
    assert not _lifecycle_sets(source, {1}, path=owner)


_INTENT_PATH = "control/src/vonk_control/recipe_operations/persistence.py"


@pytest.mark.parametrize(
    "source",
    [
        "ordinal = max(node.workload_intent_ordinal for node in nodes) + 1\n",
        "ordinal = 1 + max((n.workload_intent_ordinal for n in nodes), default=0)\n",
        "for node in nodes:\n    node.workload_intent_ordinal = ordinal\n",
        "target_node.workload_intent_ordinal += 1\n",
        "spark.workload_intent_ordinal = 9\n",
        "n.workload_intent_ordinal = 9\n",
        "nodes[0].workload_intent_ordinal = 9\n",
        "a.workload_intent_ordinal, b.workload_intent_ordinal = 1, 2\n",
        "setattr(node, 'workload_intent_ordinal', 9)\n",
        "session.execute(text('UPDATE agent_nodes SET workload_intent_ordinal = 9'))\n",
    ],
)
def test_a_workload_intent_numbered_outside_its_owner_is_flagged(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_INTENT_PATH, source, lines, modes=("intent-ordinal",))
    assert not check_source(_INTENT_PATH, source, set(), modes=("intent-ordinal",))
    assert not check_source(
        "control/src/vonk_control/workload_intent.py",
        source,
        lines,
        modes=("intent-ordinal",),
    )


@pytest.mark.parametrize(
    "source",
    [
        "ordinal = allocate_workload_intent(session, ids)\n",
        "progress.workload_intent_ordinal = ordinal\n",
        "retry_parent.workload_intent_ordinal = ordinal\n",
        "seen = {n.workload_intent_ordinal for n in nodes}\n",
        "later = max(a, b) + 1\n",
    ],
)
def test_reading_or_carrying_a_workload_intent_passes(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(_INTENT_PATH, source, lines, modes=("intent-ordinal",))


_GIT_TEST_PATH = "control/tests/test_example.py"
_GIT_FREE = ("git-free-tests",)


def _all_lines(source: str) -> set[int]:
    return set(range(1, source.count("\n") + 2))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (_GIT_TEST_PATH, 'subprocess.run(["git", "rev-parse", "HEAD"], check=True)\n'),
        (_GIT_TEST_PATH, 'out = subprocess.check_output(("git", "log"))\n'),
        (_GIT_TEST_PATH, 'subprocess.run([*GIT, "init"], check=True)\n'),
        (_GIT_TEST_PATH, 'subprocess.run(GIT + ["init"], check=True)\n'),
        (_GIT_TEST_PATH, 'subprocess.run("git status", shell=True)\n'),
        (_GIT_TEST_PATH, 'subprocess.run(["bash", "-c", "cd x && git add ."])\n'),
        (_GIT_TEST_PATH, 'os.system("git init")\n'),
        (_GIT_TEST_PATH, 'git(repo, "commit", "-m", "x")\n'),
        (_GIT_TEST_PATH, 'helpers._git(repo, "init")\n'),
        (_GIT_TEST_PATH, 'env = {"GIT_AUTHOR_NAME": "a"}\n'),
        ("tests/test_example.py", 'subprocess.run(["git", "init"])\n'),
        (
            "control/web/src/staged-code.test.ts",
            'execFileSync("git", ["init"], { cwd });\n',
        ),
        ("web/scripts/check.test.mjs", "spawnSync('git', ['add', '.']);\n"),
        ("tests/nodes/test_example.sh", 'rev="$(git rev-parse HEAD)"\n'),
        ("tests/nodes/test_example.sh", "cd repo && git init -q\n"),
        ("control/tests/example.rs", 'let out = Command::new("git").output();\n'),
    ],
)
def test_a_test_that_runs_git_is_flagged(path: str, source: str) -> None:
    assert check_source(path, source, _all_lines(source), modes=_GIT_FREE)
    assert not check_source(path, source, set(), modes=_GIT_FREE)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        # A stub file named git, a patch target and argv data are not git runs.
        (_GIT_TEST_PATH, 'stub = tmp_path / "git"\n'),
        (_GIT_TEST_PATH, 'monkeypatch.setattr(module, "git", fake)\n'),
        (_GIT_TEST_PATH, 'argv = ["git", "push"]\nassert plan(argv)\n'),
        (_GIT_TEST_PATH, 'subprocess.run(["echo", "git status"])\n'),
        (_GIT_TEST_PATH, 'env = {"VONK_GIT_SIGNING_KEY_FILE": "k"}\n'),
        (_GIT_TEST_PATH, 'text = "git show -s --format=%ct"\n'),
        (
            "tests/nodes/test_example.sh",
            "# git is only the CI edge\necho digit status\n",
        ),
        ("control/web/src/staged-code.test.ts", '// execFileSync("git")\n'),
        # Production code is not a test.
        ("control/src/vonk_control/example.py", 'subprocess.run(["git", "init"])\n'),
        ("scripts/example.sh", "git rev-parse HEAD\n"),
        ("control/src/example.rs", 'let out = Command::new("git").output();\n'),
        ("control/web/src/staged-code.ts", 'execFileSync("git", ["init"]);\n'),
    ],
)
def test_git_free_guard_leaves_other_lines_alone(path: str, source: str) -> None:
    assert not check_source(path, source, _all_lines(source), modes=_GIT_FREE)


def test_rust_git_is_flagged_only_inside_cfg_test_items() -> None:
    source = (
        'fn production() { Command::new("git"); }\n'
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    fn t() { Command::new("git"); }\n'
        "}\n"
    )
    path = "control/src/example.rs"
    assert check_source(path, source, {4}, modes=_GIT_FREE)
    assert not check_source(path, source, {1}, modes=_GIT_FREE)

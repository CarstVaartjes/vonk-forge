"""Each fixture proves a new violation fails while untouched source is ignored."""

import pytest

from .added_line_guards import AGENT_REQUESTS, added_lines, check_source

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
        "deadline = time.monotonic() + 2\n",
        "LIMIT = 1\nfuture.result(LIMIT)\n",
        "@pytest.mark.timeout(3)\ndef test_x():\n    pass\n",
        "assert time.monotonic() - started < 0.5\n",
        "assert 0.3 <= elapsed < 2\n",
        # Popen.wait raises on expiry, even in a cleanup loop.
        "for child in children:\n    child.wait(timeout=5)\n",
        # The opt-out must name its reason.
        "future.result(timeout=1)  # wall-clock:\n",
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


def test_contact_clock_guard_preserves_staleness_and_security_and_agent_freshness():
    source = (
        "def online(node, current):\n"
        "    seen = node.last_seen_at\n"
        "    return current - seen > window\n"
        "def certificate(cert, now):\n"
        "    return cert.not_before > now\n"
        "def observation(sample, now):\n"
        "    age = now - sample.observed_at\n"
        "    return timedelta(0) <= age < window\n"
    )
    assert not check_source(PATH, source, set(range(1, 9)), modes=("contact-clocks",))


@pytest.mark.parametrize(
    "call",
    [
        "self._assessment_provider",
        "self._run_switch.inspect_request",
        "self._run_switch.recheck_resources_in_session",
    ],
)
@pytest.mark.parametrize("caught", ["ValueError", "(KeyError, TypeError, ValueError)"])
def test_planner_defects_cannot_be_added_as_preparation_waits(call, caught):
    """Catch a new planner call or broad handler swallowing ValidationError."""
    source = f"try:\n    plan = {call}(request)\nexcept {caught}:\n    wait()\n"
    for line in (2, 3):
        assert check_source(PATH, source, {line}, modes=("planner-defects",))
    assert not check_source(PATH, source, {1}, modes=("planner-defects",))
    separated = source.replace(
        f"except {caught}:",
        "except ValidationError as error:\n    raise planner_contract_defect(assignment.id, error) from error\n"
        + f"except {caught}:",
    )
    assert not check_source(
        PATH, separated, set(range(1, 8)), modes=("planner-defects",)
    )

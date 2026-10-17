"""Each fixture names a violation and its principle-preserving counterpart."""

from __future__ import annotations

import ast

import pytest

from . import principle_guards, unproven_run_failures
from .principle_guards import (
    ROOT,
    rust_test_files,
    scan_retention,
    scan_rust,
    scan_rust_fixed_caps,
    scan_rust_remedies,
    scan_shell,
    scan_source,
)


@pytest.mark.parametrize(
    "mode,bad,good",
    [
        (
            "waits",
            "while True:\n    sleep(1)",
            (
                "while monotonic() < stall_deadline(started, progressed, WINDOW):\n"
                "    sleep(1)"
            ),
        ),
        (
            "waits",
            "while monotonic() < deadline:\n    sleep(1)",
            "while progress_verdict(**window) is not Verdict.STALLED:\n    sleep(1)",
        ),
        ("waits", "subprocess.run(['x'])", "subprocess.run(['x'], timeout=10)"),
        ("waits", "event.wait()", "event.wait(timeout=10)"),
        (
            "reads",
            "@router.get('/x')\ndef read():\n    raise HTTPException(status_code=503)",
            "@router.get('/x')\ndef read():\n    raise HTTPException(status_code=404)",
        ),
        (
            "reads",
            "@router.get('/x')\ndef read():\n    return JSONResponse(status_code=409)",
            "@router.post('/x')\ndef write():\n    raise HTTPException(status_code=409)",
        ),
        (
            "remedies",
            "reason = 'Prepare the cache. Retry manually.'",
            "reason = 'Cache preparation is pending.'",
        ),
        (
            "tests",
            "def test_restart():\n    assert op.state == 'needs-operator'",
            "def test_restart():\n    assert op.state != 'needs-operator'",
        ),
        (
            "tests",
            "def test_read():\n    response = client.get('/x')\n    assert response.status_code == 503",
            "def test_write():\n    response = client.post('/x')\n    assert response.status_code == 503",
        ),
        (
            "tests",
            "def test_timeout():\n    assert op.state == 'failed'",
            "def test_timeout():\n    assert op.state == 'failed'\n    assert op.reason_code == 'security.denied'",
        ),
        (
            "tests",
            "def test_stale():\n    assert route.state == 'withdrawn'",
            "def test_running():\n    assert route.state != 'withdrawn'",
        ),
    ],
)
def test_violation_and_safe_counterpart(mode, bad, good):
    assert scan_source(bad, path="sample.py", mode=mode)
    assert not scan_source(good, path="sample.py", mode=mode)


def test_bounded_unknown_ending_requires_the_fresh_request_assertion():
    failed = "def test_timeout():\n    assert op.state == 'failed'\n"
    assert scan_source(failed, path="sample.py", mode="tests")
    assert not scan_source(
        failed + "    assert_ended_without_blocking(world, op, end=end, fresh=start)\n",
        path="sample.py",
        mode="tests",
    )


def test_rust_poll_loop_requires_bound_in_its_own_body():
    assert scan_rust("fn serve() { loop { sleep(delay); } }", path="agent.rs")
    assert not scan_rust(
        "fn serve() { loop { if now > deadline { break; } } }", path="agent.rs"
    )
    assert scan_rust('fn serve() { loop { log!("deadline"); } }', path="agent.rs")


def test_a_progress_window_bounds_a_rust_loop():
    """A loop that ends once its transfer stops moving is bounded (P1)."""
    loop_body = (
        "fn read() { while let Some(c) = next_chunk(body, &inactivity) { save(c); } }"
    )
    assert not scan_rust(loop_body, path="agent.rs")
    assert scan_rust(loop_body.replace("&inactivity", "&reader"), path="agent.rs")


def test_retention_links_bulk_and_instance_deletion_but_not_unrelated_deletes():
    model = ast.parse("class History(Base):\n    __tablename__ = 'history'")
    assert scan_retention([("models.py", model)])
    for source in (
        "def prune(s):\n    s.execute(delete(History))",
        "def prune(s):\n    row = s.get(History, key)\n    s.delete(row)",
        "sql = 'DELETE FROM history WHERE created_at < cutoff'",
    ):
        assert not scan_retention(
            [("models.py", model), ("prune.py", ast.parse(source))]
        )
    assert scan_retention(
        [
            ("models.py", model),
            ("prune.py", ast.parse("def prune(s):\n    s.execute(delete(Other))")),
        ]
    )


def test_rust_remedy_messages_have_the_same_guard():
    assert scan_rust_remedies(
        'fn report() { reason("Retry manually"); }', path="agent.rs"
    )
    assert not scan_rust_remedies(
        'fn report() { reason("Awaiting observation"); }', path="agent.rs"
    )


def test_none_is_not_a_wait_bound():
    for source in ("event.wait(None)", "subprocess.run(['x'], timeout=None)"):
        assert scan_source(source, path="script.py", mode="waits")


def test_get_refusal_through_a_local_helper_is_not_invisible():
    source = "def unavailable():\n    raise HTTPException(503)\n@router.get('/x')\ndef read():\n    return unavailable()"
    assert scan_source(source, path="api.py", mode="reads")
    assert not scan_source(source.replace("503", "404"), path="api.py", mode="reads")


def test_shell_poll_loops_are_guarded():
    assert scan_shell("until ready; do sleep 1; done", path="tool.sh")
    assert scan_shell("while true; do sleep 1; done # deadline", path="tool.sh")
    assert not scan_shell(
        "while [ $SECONDS -lt $deadline ]; do sleep 1; done", path="tool.sh"
    )


@pytest.mark.parametrize(
    "message",
    [
        "Prepare the cache",
        "Run the load again",
        "Review the plan again",
        "Retry",
        "Contact support",
        "recover manually",
    ],
)
def test_every_imperative_remedy_spelling_is_guarded(message):
    assert scan_source(f"message = {message!r}", path="reason.py", mode="remedies")


def test_rust_wait_requires_timeout_wrapper():
    assert scan_rust("fn stop() { child.wait(); }", path="agent.rs")
    assert not scan_rust(
        "fn stop() { timeout(limit, child.wait()).await; }", path="agent.rs"
    )
    assert not scan_rust("fn stop() { child.wait_timeout(limit); }", path="agent.rs")


def test_rust_path_join_is_not_a_thread_wait():
    assert not scan_rust('fn path() { root.join("file"); }', path="path.rs")


def test_imported_subprocess_calls_cannot_escape_the_timeout_guard():
    for prefix, call in (
        ("from subprocess import run", "run"),
        ("from subprocess import run as execute", "execute"),
        ("import subprocess as sp", "sp.run"),
    ):
        assert scan_source(f"{prefix}\n{call}(['x'])", path="script.py", mode="waits")
        assert not scan_source(
            f"{prefix}\n{call}(['x'], timeout=5)", path="script.py", mode="waits"
        )


def test_remedy_literals_are_decoded_before_matching():
    assert scan_source('message = "\\u0052etry"', path="reason.py", mode="remedies")
    assert scan_source(
        'message = "Run path.to.tool again"', path="reason.py", mode="remedies"
    )


def test_negated_outcome_assertions_do_not_pin_the_bad_state():
    for expression in (
        "not (op.state == 'needs-operator')",
        "not (response.status_code == 503)",
        "op.state is not State.NEEDS_OPERATOR",
    ):
        source = f"def test_read():\n    response = client.get('/x')\n    assert {expression}"
        assert not scan_source(source, path="test_read.py", mode="tests")


def test_ending_inventory_requires_a_later_fresh_operation():
    bad = "def test_cancel():\n    world.start(key='old')\n    world.cancel(op)"
    good = bad + "\n    world.start(key='new')"
    helper = "def test_cancel():\n    assert_ended_without_blocking(world, op, end=world.cancel, fresh=world.start)"
    assert scan_source(bad, path="test_service.py", mode="tests")
    assert not scan_source(good, path="test_service.py", mode="tests")
    assert not scan_source(helper, path="test_service.py", mode="tests")


def test_sqlalchemy_hook_cleanup_is_not_a_service_lifecycle_ending():
    for imported, call in (
        ("from sqlalchemy import event", "event.remove"),
        ("from sqlalchemy import event as hooks", "hooks.remove"),
        ("import sqlalchemy as sa", "sa.event.remove"),
        ("from sqlalchemy.event import remove", "remove"),
    ):
        source = f"{imported}\ndef test_hook():\n    {call}(engine, 'before_cursor_execute', deny)"
        assert not scan_source(source, path="test_hook.py", mode="tests")
        assert scan_source(
            source + "\n    service.remove(operation)",
            path="test_hook.py",
            mode="tests",
        )
    for source in (
        "def test_hook():\n    event.remove(operation)",
        "from sqlalchemy import event\ndef test_hook(event):\n    event.remove(operation)",
        "from sqlalchemy import event\ndef test_hook():\n    event = service\n    event.remove(operation)",
        "from sqlalchemy import event\nevent = service\ndef test_hook():\n    event.remove(operation)",
    ):
        assert scan_source(source, path="test_hook.py", mode="tests")


def test_real_activation_or_submission_is_fresh_admission_after_ending():
    """Actual Controller admission APIs establish a new owner after Stop."""
    for admission in ("activate_job_run", "submit"):
        source = f"def test_recovery():\n    operations.stop(run)\n    service.{admission}(plan)"
        assert not scan_source(source, path="test_recovery.py", mode="tests")
    assert scan_source(
        "def test_read_only():\n    operations.stop(run)\n    service.preview_run(run)",
        path="test_recovery.py",
        mode="tests",
    )


def test_compose_embedded_shell_requires_a_bound_in_the_executed_loop():
    from .principle_guards import scan_compose_shell

    source = """services:
  consumer:
    entrypoint: [/bin/sh, -c, 'until [ -f /assets/ready ]; do sleep 1; done']
    environment: {COMMENT: timeout}
"""
    assert scan_compose_shell(source, path="compose.yaml")
    assert scan_compose_shell(
        source.replace("do sleep 1", "do echo timeout; sleep 1"), path="compose.yaml"
    )
    bounded = source.replace(
        "do sleep 1; done",
        'do attempts=$$((attempts + 1)); if [ "$$attempts" -ge 120 ]; then exit 1; fi; sleep 1; done',
    )
    assert not scan_compose_shell(bounded, path="compose.yaml")


def test_observation_loop_requires_resolved_unswallowed_absolute_deadline():
    path = "src/cluster_profiles/observation_transfer_reader.py"
    guard = (
        "import math\nimport time\n"
        "class ObservationTransferInvalid(ValueError): pass\n"
        "def check_observation_deadline(deadline):\n"
        "    if not math.isfinite(deadline):\n"
        "        raise ObservationTransferInvalid('invalid')\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise TimeoutError('elapsed')\n"
    )
    receiver = (
        "def receive_observation(*, deadline):\n"
        "    while True:\n"
        "        check_observation_deadline(deadline)\n"
        "        consume()\n"
    )
    source = guard + receiver
    assert not scan_source(source, path=path, mode="waits")
    swallowed = (
        "def receive_observation(*, deadline):\n"
        "    try:\n"
        "        while True:\n"
        "            check_observation_deadline(deadline)\n"
        "            consume()\n"
        "    except TimeoutError:\n"
        "        pass\n"
    )
    for changed in (
        source.replace("        check_observation_deadline(deadline)\n", ""),
        guard
        + receiver.replace(
            "check_observation_deadline(deadline)", "different_guard(deadline)"
        ),
        source.replace("raise TimeoutError('elapsed')", "return"),
        source.replace(
            "def receive_observation(*, deadline):",
            "def receive_observation(*, deadline, check_observation_deadline):",
        ),
        source.replace(
            "        consume()", "        deadline += 10\n        consume()"
        ),
        guard + swallowed,
        source.replace("import time", "import replacement as time"),
        source.replace(
            "        consume()",
            "        import replacement as deadline\n        consume()",
        ),
        source.replace(
            "        consume()", "        from replacement import *\n        consume()"
        ),
    ):
        assert scan_source(changed, path=path, mode="waits")
    assert scan_source(source, path="unrelated_reader.py", mode="waits")


@pytest.mark.parametrize("subject", ["report", "reports[0]"])
def test_failure_report_is_evidence_not_a_terminal_operation(subject):
    """Do not mistake a persisted diagnostic for a poisoned operation outcome."""
    source = (
        "import json\ndef test_startup_timeout():\n"
        "    report = json.loads(path.read_text())\n"
        "    reports.append(json.loads(path.read_text()))\n"
        f"    assert {subject}['status'] == 'failed'\n"
        f"    assert {subject}['failure']['cause']\n"
    )
    assert not scan_source(source, path="test_diagnostics.py", mode="tests")
    # Ordinary job state is still a violation, even beside a valid report.
    assert scan_source(
        source + "    assert op.state == 'failed'\n",
        path="test_diagnostics.py",
        mode="tests",
    )
    for changed in (
        source.replace("json.loads(path.read_text())", "operation.result"),
        source.replace("['status']", "['state']"),
        source.replace(f"    assert {subject}['failure']['cause']\n", ""),
        source.replace(
            f"assert {subject}['failure']['cause']", "assert other['failure']['cause']"
        ),
        source.replace(
            f"assert {subject}['failure']['cause']",
            f"assert not {subject}['failure']['cause']",
        ),
    ):
        assert scan_source(changed, path="test_diagnostics.py", mode="tests")


def test_nested_package_get_helper_refusal_follows_concrete_import(
    tmp_path, monkeypatch
):
    """Moving a GET's rejecting helper cannot erase its existing refusal finding."""
    from . import principle_guards as guards

    root = tmp_path / "control/src/vonk_control"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    package = root / "api"
    package.mkdir()
    (package / "common.py").write_text(
        "def required():\n    raise HTTPException(status_code=503)\n"
    )
    route = "from .common import required\n@router.get('/x')\ndef observe():\n    return required()\n"
    path = "control/src/vonk_control/api/application.py"
    (tmp_path / path).write_text(route)
    monkeypatch.setattr(guards, "ROOT", tmp_path)
    assert guards.scan_source(route, path=path, mode="reads")
    (package / "common.py").write_text("def required():\n    return 1\n")
    assert not guards.scan_source(route, path=path, mode="reads")


def test_refusal_assertion_follows_its_http_response_producer():
    """Catches treating an unrelated database get as a refused HTTP read."""
    write = "def test_submit():\n    response = client.post('/renew')\n    row = session.get(Certificate, serial)\n    assert response.status_code == 503\n"
    read = write.replace("client.post", "client.get")
    assert not scan_source(write, path="test_enrollment.py", mode="tests")
    assert scan_source(read, path="test_enrollment.py", mode="tests")
    direct = "def test_read():\n    assert client.get('/status').status_code == 503\n"
    assert scan_source(direct, path="test_enrollment.py", mode="tests")


def test_bounded_ending_requires_distinct_fresh_execution_and_completion():
    ending = "def test_storage_unavailable():\n    assert service.get(old.id).state == LifecycleState.FAILED\n"
    admitted = (
        ending
        + "    fresh = service.start(request_id='fresh')\n    assert fresh.id != old.id\n"
    )
    observed = (
        admitted
        + "    service.run_pending()\n    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED\n"
    )
    assert scan_source(ending, path="test_owner.py", mode="tests")
    assert scan_source(admitted, path="test_owner.py", mode="tests")
    assert not scan_source(observed, path="test_owner.py", mode="tests")
    assert scan_source(
        observed.replace("fresh.id != old.id", "fresh.id == old.id"),
        path="test_owner.py",
        mode="tests",
    )
    assert scan_source(
        observed.replace("    service.run_pending()\n", ""),
        path="test_owner.py",
        mode="tests",
    )


@pytest.mark.parametrize("fault", (False, True))
@pytest.mark.parametrize("collecting", (False, True))
def test_inventory_restores_gc_policy_on_every_exit(monkeypatch, fault, collecting):
    """Catches scanner failure leaking a disabled collector into later work."""
    import gc

    from . import principle_guards

    original = gc.isenabled()
    try:
        gc.enable() if collecting else gc.disable()

        def scan(_mode):
            assert not gc.isenabled()
            if fault:
                raise OSError("inventory unavailable")
            return []

        monkeypatch.setattr(principle_guards, "_scan_sites", scan)
        if fault:
            with pytest.raises(Exception):  # noqa: B017 -- ending witness; GC ownership is asserted below
                principle_guards.scan_sites("tests")
        else:
            assert principle_guards.scan_sites("tests") == []
        assert gc.isenabled() == collecting
    finally:
        gc.enable() if original else gc.disable()


@pytest.mark.parametrize("failure_at", (None, 8))
def test_inventory_releases_temporary_trees_without_scanning_old_generations(
    tmp_path, monkeypatch, failure_at
):
    """Catches retained parsed trees, including an interrupted inventory."""
    import gc
    import weakref

    from . import principle_guards

    sources = []
    for index in range(16):
        source = tmp_path / f"test_fixture_{index}.py"
        source.write_text("def test_fixture():\n    assert True\n")
        sources.append(source)
    monkeypatch.setattr(principle_guards, "ROOT", tmp_path)
    monkeypatch.setattr(principle_guards, "source_files", lambda _mode: sources)
    trees = []
    parse = principle_guards.parse_file

    def observe_parse(path, **kwargs):
        if failure_at is not None and len(trees) == failure_at:
            raise OSError("inventory unavailable")
        parsed = parse(path, **kwargs)
        trees.append(weakref.ref(parsed.tree))
        return parsed

    full_collections = []
    collect = gc.collect

    def observe_collection(generation=2):
        if generation > 0:
            full_collections.append(generation)
        return collect(generation)

    monkeypatch.setattr(principle_guards, "parse_file", observe_parse)
    monkeypatch.setattr(gc, "collect", observe_collection)
    if failure_at is None:
        assert principle_guards.scan_sites("tests") == []
        assert len(trees) == len(sources)
    else:
        with pytest.raises(Exception):  # noqa: B017 -- ending witness; retained trees asserted below
            principle_guards.scan_sites("tests")
        assert len(trees) == failure_at
    assert all(tree() is None for tree in trees)
    assert not full_collections
    # A new scan is admitted after both success and failure.
    trees.clear()
    failure_at = None
    assert principle_guards.scan_sites("tests") == []
    assert all(tree() is None for tree in trees)


def test_installation_is_fresh_admission_after_cancellation():
    """A real install request is admission; preview alone is not."""
    ended = "def test_cancel():\n    service.cancel(old.id)\n"
    assert scan_source(ended, path="test_owner.py", mode="tests")
    assert scan_source(
        ended + "    service.preview_install(plan)\n",
        path="test_owner.py",
        mode="tests",
    )
    assert not scan_source(
        ended + "    fresh = service.install(plan, request_id=fresh_id)\n",
        path="test_owner.py",
        mode="tests",
    )


_FIXED_CAPS = [
    # A stored instant of the work plus a constant, ending it.
    """
def tick(job, now):
    if now >= job.created_at + timedelta(seconds=LIMIT):
        fail(job)
""",
    # Elapsed since an instant of the work, past a constant.
    """
def tick(progress, now):
    since = progress.storage_wait_since
    if now - since > timedelta(seconds=LIMIT):
        raise Refused()
""",
    # The deadline named first, then compared.
    """
def tick(job, now):
    deadline = _aware(job.created_at) + timedelta(seconds=LIMIT)
    if now >= deadline:
        code = RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
""",
    # A persisted deadline written once from the clock, ended on elsewhere.
    """
def settle(row, now):
    row.recovery_deadline = now + BUDGET
def tick(row, now):
    if now >= row.recovery_deadline:
        mark_failed(row)
""",
]
_STALL_WINDOWS = [
    # A stall window over the work's own progress and the owners it waits on.
    """
def tick(job, now):
    verdict = progress_verdict(
        started_at=job.created_at, progressed_at=job.progressed_at,
        window=WINDOW, now=now, waiting_on=owners,
    )
    if verdict is Verdict.STALLED:
        fail(job)
""",
    """
def settle(row, now):
    row.recovery_deadline = stall_deadline(now, None, BUDGET)
def tick(row, now):
    if now >= row.recovery_deadline:
        mark_failed(row)
""",
    # Before the cap only the waiting path runs; nothing ends there.
    """
def tick(view, now):
    retry_at = view.created_at + QUIET
    if now < retry_at:
        return make_blocker(Code.PREPARATION_FAILED, "asks again later")
""",
    # Freshness of evidence is not the age of the work.
    """
def accept(sample, now):
    if now - sample.observed_at > timedelta(minutes=5):
        raise ValueError("stale sample")
""",
    # A cap that resets for another round never ends the work.
    """
def maintain(owner, now):
    if now >= owner.reconciliation_deadline_at:
        owner.reconciliation_deadline_at = now + timedelta(minutes=5)
""",
]


_SETUP_SPENT_BUDGETS = [
    # The inventory runs on the budget, so a slow one ends every pass first.
    """
def collect(self):
    deadline = time.monotonic() + 2.0
    with self._sessions() as session:
        protected = inventory(session)
    for row in rows:
        if time.monotonic() >= deadline:
            return
""",
    # The page query runs on the budget before its first row.
    """
def convert(sessions):
    deadline = time.monotonic() + SCAN_SECONDS
    converted = 0
    with sessions.begin() as session:
        rows = session.scalars(page)
        for row in rows:
            if time.monotonic() >= deadline:
                break
""",
]
_WORK_BUDGETS = [
    # The budget starts after the inventory.
    """
def collect(self):
    with self._sessions() as session:
        protected = inventory(session)
    deadline = time.monotonic() + 2.0
    for row in rows:
        if time.monotonic() >= deadline:
            return
""",
    # A retry's first attempt is its work, not setup.
    """
def admit(cache):
    deadline = time.monotonic() + BUDGET
    while True:
        try:
            with cache.lock, cache.session() as session:
                return attempt(session)
        except Busy:
            if time.monotonic() >= deadline:
                raise
""",
    # A context that the budget itself bounds.
    """
def publish(storage):
    deadline = time.monotonic() + LOCK_SECONDS
    with storage.lock(deadline):
        pass
""",
]


@pytest.mark.parametrize("source", _SETUP_SPENT_BUDGETS)
def test_a_pass_budget_spent_on_setup_fails_the_waits_guard(source):
    sites = scan_source(source, path="control/src/sample.py", mode="waits")
    assert "pass-budget-spent-on-setup" in {site.kind for site in sites}


@pytest.mark.parametrize("source", _WORK_BUDGETS)
def test_a_pass_budget_that_starts_at_the_work_passes_the_waits_guard(source):
    sites = scan_source(source, path="control/src/sample.py", mode="waits")
    assert "pass-budget-spent-on-setup" not in {site.kind for site in sites}


@pytest.mark.parametrize("source", _FIXED_CAPS)
def test_a_fixed_cap_that_ends_work_fails_the_waits_guard(source):
    sites = scan_source(source, path="control/src/sample.py", mode="waits")
    assert "fixed-cap-ends-work" in {site.kind for site in sites}


@pytest.mark.parametrize("source", _STALL_WINDOWS)
def test_a_stall_window_or_a_non_ending_bound_passes_the_waits_guard(source):
    sites = scan_source(source, path="control/src/sample.py", mode="waits")
    assert "fixed-cap-ends-work" not in {site.kind for site in sites}


def test_a_loop_over_data_is_not_a_wait():
    source = (
        "while pending:\n    item = pending.pop()\n    parts.append(', '.join(item))"
    )
    assert not scan_source(source, path="control/src/sample.py", mode="waits")


def test_no_fixed_cap_ends_work_anywhere_in_the_controller_or_cli():
    """Whole-tree: a wait never expires; only a stall ends work (P1)."""
    assert principle_guards.fixed_cap_sites() == []


FIXED_CAPS = [
    # A client-wide total cuts every streamed body it ever carries.
    (
        "fn build() { Client::builder().connect_timeout(T).timeout(LIMIT).build() }",
        "fn build() { Client::builder().connect_timeout(T).build() }",
    ),
    # A per-chunk read against a deadline fixed at the start of the transfer.
    (
        (
            "fn download() { let deadline = Instant::now() + CONTROLLER_REQUEST_TIMEOUT;\n"
            "while let Some(c) = timeout_at(deadline, response.chunk()).await {} }"
        ),
        (
            "fn download() { let inactivity = Inactivity::new(TRANSFER_INACTIVITY);\n"
            "while let Some(c) = next_chunk(response.chunk(), &inactivity).await? {} }"
        ),
    ),
    # A budget scaled by size still fails a slow link that keeps moving.
    (
        "fn prepare() { let budget = Duration::from_secs(75 + bytes.div_ceil(MIB)); }",
        "fn prepare() { let stall = StallWindow::new(window); }",
    ),
    # A total on an upload body.
    (
        "fn upload() { client.put(url).timeout(UPLOAD).body(Body::wrap_stream(s)).send() }",
        "fn upload() { send_streaming(client.put(url).body(Body::wrap_stream(s)), &i) }",
    ),
    # A copy whose progress is reported is held to its progress, not a clock.
    (
        (
            "fn copy() { let end = Instant::now() + Duration::from_secs(600);\n"
            "set_progress_bytes(fence, done, total); }"
        ),
        (
            "fn copy() { let copied = Inactivity::new(COPY_INACTIVITY);\n"
            "set_progress_bytes(fence, done, total); copied.observe(); }"
        ),
    ),
]


@pytest.mark.parametrize("bad,good", FIXED_CAPS)
def test_a_transfer_ends_on_inactivity_never_on_a_fixed_total(bad, good):
    assert scan_rust(bad, path="agent.rs")
    assert not scan_rust_fixed_caps(good, path="agent.rs")


def test_a_small_exchange_keeps_its_own_total():
    """A heartbeat or probe moves a few KiB: its total is its bound."""
    source = (
        "fn heartbeat() { client.post(url).timeout(HEARTBEAT_REQUEST_TIMEOUT)"
        ".body(body).send().await; bounded_body(response).await }\n"
        "fn upload() { client.head(url).timeout(LIMIT).send().await;\n"
        "send_streaming(client.put(url).body(Body::wrap_stream(s)), &i).await }"
    )
    assert not scan_rust_fixed_caps(source, path="agent.rs")


def test_test_code_may_use_fixed_totals():
    source = (
        "#[cfg(test)]\nmod tests {\n"
        "fn build() { Client::builder().timeout(LIMIT).build() }\n}\n"
    )
    assert not scan_rust_fixed_caps(source, path="agent.rs")
    assert not scan_rust_fixed_caps(
        "#![cfg(test)]\n" + FIXED_CAPS[0][0], path="tests_0.rs"
    )


def test_every_agent_transfer_ends_on_inactivity():
    """Whole tree: no fixed total ends an agent transfer (principle P1)."""
    crate = ROOT / "rust/crates/vonk-agent/src"
    tests = rust_test_files(crate)
    production = [path for path in sorted(crate.rglob("*.rs")) if path not in tests]
    assert len(production) > 50 and len(tests) > 20  # the scan sees the crate
    sites = [
        site
        for path in production
        for site in scan_rust_fixed_caps(
            path.read_text(), path=path.relative_to(ROOT).as_posix()
        )
    ]
    assert sites == []


def _admissions_without_a_patient_owner(source: str) -> list[int]:
    """Lines where profile admission runs neither patient nor handing a busy
    refusal to the pending-admission owner, which queues for its locks."""
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    def owned(node: ast.AST) -> bool:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call)
                and ast.unparse(item.context_expr.func).endswith("patient_admission")
                for item in node.items
            ):
                return True
            if isinstance(node, ast.Try) and any(
                "_defer_pending_application" in ast.unparse(handler)
                for handler in node.handlers
            ):
                return True
        return False

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_queue_application"
        and not owned(node)
    ]


def test_a_retried_admission_queues_for_its_locks_or_hands_them_over():
    """A try-lock never queues: an owner that retries admission on its own
    clock, against node rows heartbeats keep writing, loses every try unless it
    becomes patient (P: a wait never blocks forever)."""
    bad = "try:\n    self._queue_application(p)\nexcept Busy:\n    self._park(p)\n"
    patient = "with patient_admission(n):\n    self._queue_application(p)\n"
    handed = (
        "try:\n    self._queue_application(p)\n"
        "except Busy:\n    self._defer_pending_application(i)\n"
    )
    assert _admissions_without_a_patient_owner(bad) == [2]
    assert _admissions_without_a_patient_owner(patient) == []
    assert _admissions_without_a_patient_owner(handed) == []
    sources = sorted((ROOT / "control/src/vonk_control").rglob("*.py"))
    sites = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sources
        for line in _admissions_without_a_patient_owner(path.read_text())
    ]
    assert len(sources) > 100  # the scan sees the Controller
    assert sites == []


_UNPROVEN_FAILURES = [
    # A Stop that was not confirmed ends the run, so the ownership rule frees
    # ports and memory a running container still uses.
    "def finish(run, failed):\n    run.state = RunState.FAILED if failed else RunState.STOPPED",
    # A rank whose Stop did not succeed is not proven gone either.
    "def project(node, ok):\n    node.state = RunState.STOPPED if ok else RunState.FAILED",
    # The proof must be the function's own: a nested helper does not count.
    (
        "def settle(run, nodes, now):\n"
        "    def check():\n        return run_node_reports_absent(run, nodes[0], now)\n"
        "    run.state = RunState.FAILED"
    ),
]
_PROVEN_FAILURES = [
    # Failed only when every rank's own Spark reports it gone, now.
    (
        "def settle(run, nodes, now):\n"
        "    gone = all(run_node_reports_absent(run, n, now) for n in nodes)\n"
        "    run.state = RunState.FAILED if gone else RunState.LOST"
    ),
    # The Spark's report being applied is the evidence.
    "def apply(node, evidence):\n    node.state = RunState.RUNNING if evidence.process_running else RunState.FAILED",
    # An unknown effect is lost, and a comparison is not an assignment.
    "def finish(run):\n    run.state = RunState.LOST\n    return run.state == RunState.FAILED",
]


@pytest.mark.parametrize("source", _UNPROVEN_FAILURES)
def test_a_failed_run_or_rank_without_fresh_absence_fails_the_guard(source):
    assert unproven_run_failures.unproven_failures(ast.parse(source))


@pytest.mark.parametrize("source", _PROVEN_FAILURES)
def test_a_failed_run_with_fresh_absence_or_a_lost_one_passes_the_guard(source):
    assert not unproven_run_failures.unproven_failures(ast.parse(source))


def test_no_run_or_rank_is_failed_without_fresh_absence_anywhere():
    """Whole tree: an unknown effect is lost, never failed (Class 2)."""
    assert unproven_run_failures.tree_sites() == []


def test_the_agent_stops_a_job_only_after_reobserving_it():
    """Whole tree: a job whose helper answer was lost is re-observed by its
    identity before anything stops it (Class 2). Its stop on error has one
    caller, the re-observation, which stops only on a cancellation, the job's
    deadline, or a helper that cannot re-observe."""
    crate = ROOT / "rust/crates/vonk-agent/src"
    tests = rust_test_files(crate)
    callers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in crate.rglob("*.rs")
        if path not in tests and ".stop_job(" in "".join(path.read_text().split())
    )
    assert callers == ["rust/crates/vonk-agent/src/executor/jobs/reobserve.rs"]


def _constant_spark_observes(source: str) -> list[int]:
    """Lines of an ``observe`` that never reads its row or its store.

    Scoped to modules that import the Spark attempt (``AgentOperationAttempt``): an
    adapter there observes an effect a Spark executed, so a constant answer
    turns a missing observation into an outcome (Class 2).  A parent that
    aggregates its children elsewhere may answer ``unknown`` and let them
    observe.
    """

    tree = ast.parse(source)
    if not any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "AgentOperationAttempt" for alias in node.names)
        for node in ast.walk(tree)
    ):
        return []
    lines = []
    for owner in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for method in owner.body:
            if not isinstance(method, ast.FunctionDef) or method.name != "observe":
                continue
            params = {arg.arg for arg in method.args.args}
            read = {
                node.id
                for node in ast.walk(method)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            if not params & read:
                lines.append(method.lineno)
    return lines


_SPARK_MODULE = "from .models import AgentOperationAttempt\n"


def test_a_constant_observe_of_a_spark_order_fails_the_guard():
    constant = (
        _SPARK_MODULE + "class Orders:\n"
        "    def observe(self, row):\n"
        '        """No probe."""\n'
        '        return Observed(Effect.UNKNOWN, "the effect is unobserved")\n'
    )
    reading = (
        _SPARK_MODULE + "class Orders:\n"
        "    def observe(self, row):\n"
        "        return receipt_observation(self.attempt_of(row.id))\n"
    )
    aggregate = (
        "class Batch:\n"
        "    def observe(self, row):\n"
        '        return Observed(Effect.UNKNOWN, "the batch follows its children")\n'
    )
    assert _constant_spark_observes(constant) == [3]
    assert _constant_spark_observes(reading) == []
    assert _constant_spark_observes(aggregate) == []


def test_no_spark_dispatching_adapter_observes_a_constant():
    """Whole tree: a Spark order reads the observation of its exact attempt."""
    sources = sorted((ROOT / "control/src/vonk_control").rglob("*.py"))
    sites = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sources
        for line in _constant_spark_observes(path.read_text())
    ]
    assert len(sources) > 100  # the scan sees the Controller
    assert sites == []


#: Functions that order two ordinals read on one Spark, and why.
_ONE_SPARK_ORDINAL_ORDERS = {
    # The source JobRun ran on ``target.node_id`` alone, the Spark whose
    # ordinal the Stop carries (checked in the same predicate).
    (
        "control/src/vonk_control/profile_stop_authority.py",
        "validate_jobrun_stop_source",
    ),
}


def _cross_spark_ordinal_orders(source: str, path: str = "") -> list[int]:
    """Lines that order two workload ordinals, neither read off a Spark's row.

    A load fences only its own Sparks, at one more than their highest ordinal,
    so two loads' ordinals order nothing: two loads got 598 on different
    Sparks (#1608).  A Spark's current ordinal (its node row) against a load's
    is the supersession check; "newer" between loads is the acceptance order
    (``_application_order_key``).  Equality is identity, not order, and is not
    checked.  Scoped to modules that import ``FleetProfileApplication``: a load
    spans Sparks there, while a Spark order elsewhere holds one Spark's ordinal.
    """

    tree = ast.parse(source)
    if not any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "FleetProfileApplication" for alias in node.names)
        for node in ast.walk(tree)
    ):
        return []

    def ordinal(node: ast.expr) -> bool:
        name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        return name.endswith("ordinal")

    def node_row(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and "node" in ast.unparse(node.value).lower()
        )

    lines = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (path, function.name) in _ONE_SPARK_ORDINAL_ORDERS:
            continue
        for compare in ast.walk(function):
            if not isinstance(compare, ast.Compare):
                continue
            operands = [compare.left, *compare.comparators]
            for op, left, right in zip(compare.ops, operands, operands[1:]):
                if (
                    isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                    and ordinal(left)
                    and ordinal(right)
                    and not (node_row(left) or node_row(right))
                ):
                    lines.append(compare.lineno)
    return sorted(set(lines))


_LOADS = "from .models import FleetProfileApplication\n"


def test_ordering_two_loads_by_their_ordinals_fails_the_guard():
    across = (
        _LOADS + "def older(prior_progress, workload_intent_ordinal):\n"
        "    prior_ordinal = prior_progress.workload_intent_ordinal\n"
        "    return prior_ordinal >= workload_intent_ordinal\n"
    )
    by_acceptance = (
        _LOADS + "def older(prior_order, intent_order):\n"
        "    return prior_order >= intent_order\n"
    )
    on_its_spark = (
        _LOADS + "def superseded(node, ordinal, progress):\n"
        "    if progress.workload_intent_ordinal != ordinal:\n"
        "        return False\n"
        "    return node.workload_intent_ordinal > ordinal\n"
    )
    spark_orders = (
        "def older(old, operation):\n    return old.ordinal < operation.ordinal\n"
    )
    assert _cross_spark_ordinal_orders(across) == [4]
    assert _cross_spark_ordinal_orders(by_acceptance) == []
    assert _cross_spark_ordinal_orders(on_its_spark) == []
    assert _cross_spark_ordinal_orders(spark_orders) == []


def test_no_load_is_ordered_by_another_loads_ordinal_anywhere():
    """Whole tree: "newer" between loads is the acceptance order (P: the
    newest request steers)."""
    sources = sorted((ROOT / "control/src/vonk_control").rglob("*.py"))
    sites = [
        f"{path.relative_to(ROOT).as_posix()}:{line}"
        for path in sources
        for line in _cross_spark_ordinal_orders(
            path.read_text(), path.relative_to(ROOT).as_posix()
        )
    ]
    assert len(sources) > 100  # the scan sees the Controller
    assert sites == []

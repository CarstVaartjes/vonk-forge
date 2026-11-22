import pytest

from .added_line_guards import check_source
from .added_line_guards_support import _BARRIER_TEST, PATH


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
        ("control/src/vonk_control/telemetry/samples.py", "_canonical_sample"),
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
    reviewed = "control/src/vonk_control/availability_production/scheduler.py"
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
        "control/tests/test_model_cache_part1.py",
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


def test_a_restated_job_limit_is_flagged_outside_its_owner() -> None:
    line = "MAX_INPUT_FILES = 32\n"
    owner = "agent_protocol/src/vonk_agent_protocol/job_limits.py"
    copy = "control/src/vonk_control/artifact_jobs/contracts.py"
    assert check_source(copy, line, {1}, modes=("job-limits",))
    assert check_source(
        copy, "MAX_TIMEOUT_SECONDS: int = 3600\n", {1}, modes=("job-limits",)
    )
    assert not check_source(owner, line, {1}, modes=("job-limits",))
    assert not check_source(copy, "le=MAX_INPUT_FILES\n", {1}, modes=("job-limits",))


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


def test_nas_coverage_is_required_only_where_nas_bytes_are_read() -> None:
    """Catches a planner or compiler that needs the NAS copy of a model every
    target Spark already holds (a re-download after NAS eviction)."""
    source = "def receipts(cache, digest):\n    return cache.resolve_verified_artifact_set(digest)\n"
    assert check_source(
        "control/src/vonk_control/execution_plan_service.py",
        source,
        {2},
        modes=("model-sets",),
    )
    gate = "def plan(cache, manifest):\n    cache._require_managed_cache_coverage(manifest)\n"
    assert check_source(
        "control/src/vonk_control/run_switch_operations/run_planning.py",
        gate,
        {2},
        modes=("model-sets",),
    )
    # The route main's compile took: the NAS adapter with the caller's manifest.
    adapter = (
        "def verified_model_receipts(service, artifact_set_sha256, manifest):\n"
        "    source = ModelCacheObjectSource.from_service(service)\n"
        "    return source.verified_model_objects_for_set(artifact_set_sha256, manifest)\n"
    )
    assert check_source(
        "control/src/vonk_control/distribution/sources.py",
        adapter,
        {3},
        modes=("model-sets",),
    )
    # A grant built from the NAS set parked a copy that sends only the image.
    grant = "def grant(source, plan):\n    return source.objects_for_set(plan.digest)\n"
    assert check_source(
        "control/src/vonk_control/distribution/delivery.py",
        grant,
        {2},
        modes=("model-sets",),
    )
    for owner in ("model_cache/availability.py", "distribution/model_cache.py"):
        assert not check_source(
            f"control/src/vonk_control/{owner}", source, {2}, modes=("model-sets",)
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
    "recipe = RecipeDefinition.model_validate_json(canonical_message(row.document))\n",
    "model = ModelDefinition.model_validate(document, strict=True)\n",
    "field = readable_or_none(TypeAdapter(RecipeDefinition))\n",
    "recipe = contracts.RecipeDefinition.model_validate(row.document)\n",
    "field = readable_or_none(pydantic.TypeAdapter(RecipeDefinition | None))\n",
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


_SCRATCH_PATH = "control/src/vonk_control/recipe_packages/client.py"


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


_CONTRACTS_PATH = "agent_protocol/src/vonk_agent_protocol/contracts.py"


def test_a_length_term_in_a_typed_string_rule_is_flagged() -> None:
    source = (
        "def _typed_result_string(path, value):\n"
        '    return len(value) <= 2048 and "\\x00" not in value\n'
    )
    modes = ("typed-string-lengths",)
    assert check_source(_CONTRACTS_PATH, source, {2}, modes=modes)
    assert not check_source(_CONTRACTS_PATH, source, set(), modes=modes)
    ok = (
        "def _typed_result_string(path, value):\n"
        '    return len(path) == 4 and "\\x00" not in value\n'
    )
    assert not check_source(_CONTRACTS_PATH, ok, {2}, modes=modes)


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


_REVOCATION_PATH = "control/src/vonk_control/browser_auth.py"


_REVOCATION_SOURCE = "def f(row):\n    row.revoked_at = now\n{extra}"


def test_a_revocation_without_a_durable_commit_is_flagged() -> None:
    source = _REVOCATION_SOURCE.format(extra="")
    modes = ("durable-revocations",)
    assert check_source(_REVOCATION_PATH, source, {2}, modes=modes)
    # Only an added write line can fail the change.
    assert not check_source(_REVOCATION_PATH, source, {1}, modes=modes)


def test_an_added_keyword_in_a_multiline_update_is_flagged() -> None:
    source = (
        "def f(db):\n"
        "    db.execute(\n"
        "        update(Key)\n"
        "        .where(Key.id == 1)\n"
        "        .values(\n"
        "            state='revoked',\n"
        "            revoked_at=now,\n"
        "        )\n"
        "    )\n"
    )
    modes = ("durable-revocations",)
    assert check_source(_REVOCATION_PATH, source, {7}, modes=modes)
    assert not check_source(_REVOCATION_PATH, source, {3}, modes=modes)


def test_a_revocation_with_a_durable_commit_or_in_enrollment_passes() -> None:
    modes = ("durable-revocations",)
    durable = _REVOCATION_SOURCE.format(extra="    require_durable_commit(db)\n")
    assert not check_source(_REVOCATION_PATH, durable, {2}, modes=modes)
    plain = _REVOCATION_SOURCE.format(extra="")
    enrollment = "control/src/vonk_control/enrollment/service.py"
    assert not check_source(enrollment, plain, {2}, modes=modes)


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

import pytest

from .added_line_guards import check_source
from .added_line_guards_support import _BARRIER_TEST, PATH, _call, _docker_listings


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


_RETENTION_PATH = "control/src/vonk_control/unused_storage_collection/references.py"


@pytest.mark.parametrize(
    ("path", "source"),
    [
        # Every metadata-only release dropped the loaded profile's installation.
        (_RETENTION_PATH, "kept = newest[3] == revision.content_digest\n"),
        (
            _RETENTION_PATH,
            "if head.content_digest != revision.content_digest:\n    pass\n",
        ),
        (
            "control/src/vonk_control/model_cache/fleet_removal.py",
            "kept = revision.content_digest in head_digests\n",
        ),
        (
            "control/src/vonk_control/recipe_update_notice.py",
            "same = newest.content_digest == running.content_digest\n",
        ),
        (
            "control/src/vonk_control/observed_run_memory.py",
            "same = head.content_digest == old.content_digest\n",
        ),
    ],
)
def test_retention_deciding_the_head_by_content_digest_is_flagged(
    path: str, source: str
) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(path, source, lines, modes=("head-digests",))
    # Only an added line can fail the change.
    assert not check_source(path, source, set(), modes=("head-digests",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (_RETENTION_PATH, "kept = runs_identically(installed, current)\n"),
        (_RETENTION_PATH, "same = revision.id == head_revision\n"),
        # A model's own digest, not a head decision.
        (
            "control/src/vonk_control/model_cache/fleet_removal.py",
            "if current_head and revision.content_digest == digest:\n    pass\n",
        ),
        (
            "control/src/vonk_control/recipes.py",
            "kept = newest.content_digest == running.content_digest\n",
        ),
    ],
)
def test_retention_using_the_head_revision_or_execution_identity_passes(
    path: str, source: str
) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(path, source, lines, modes=("head-digests",))


@pytest.mark.parametrize(
    "source",
    [
        "rows = select(ModelCacheSet).where(ModelCacheSet.model_content_sha256 == digest)\n",
        "rows = select(ModelCacheSet).where(ModelCacheSet.model_content_sha256.in_(models))\n",
        # The Models a Spark holds, by the Model its installations were made for.
        "q = select(RecipeInstallation.model_content_sha256).join(InstallationNode, j)\n",
    ],
)
def test_a_set_matched_by_its_first_requester_is_flagged(source: str) -> None:
    path = "control/src/vonk_control/unused_storage_collection/common.py"
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(path, source, lines, modes=("set-first-requester",))
    # Only an added line can fail the change.
    assert not check_source(path, source, set(), modes=("set-first-requester",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        # The owner of the binding rule.
        (
            "control/src/vonk_control/model_cache/lineage.py",
            "q = select(ModelCacheSet).where(ModelCacheSet.model_content_sha256 == d)\n",
        ),
        # By the installation's set, and a presence check, pass anywhere.
        (
            "control/src/vonk_control/unused_storage_collection/common.py",
            "q = select(ModelCacheSet).where(ModelCacheSet.artifact_set_sha256.in_(s))\n",
        ),
        (
            "control/src/vonk_control/unused_storage_collection/common.py",
            "q = select(ModelCacheSet).where(ModelCacheSet.model_content_sha256.is_not(None))\n",
        ),
        # A Spark's installations by their set, or the Model next to other facts.
        (
            "control/src/vonk_control/disk_reservations.py",
            "q = select(RecipeInstallation.model_artifact_set_sha256).join(InstallationNode, j)\n",
        ),
        (
            "control/src/vonk_control/disk_reservations.py",
            "q = select(RecipeInstallation.id, RecipeInstallation.model_content_sha256).join(InstallationNode, j)\n",
        ),
    ],
)
def test_a_set_read_by_its_bindings_or_installation_passes(
    path: str, source: str
) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(path, source, lines, modes=("set-first-requester",))


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


@pytest.mark.parametrize(
    "source",
    [
        "same = run.alias == assignment.alias\n",
        "q = select(RecipeRun.id).where(RecipeRun.alias == alias)\n",
        "other = assignment.alias != run.alias\n",
        "same = row.alias == alias\n",
        "same = candidate.alias != other_alias\n",
    ],
)
def test_a_run_alias_compared_by_hand_is_flagged(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_ALIAS_PATH, source, lines, modes=("alias-rules",))
    assert not check_source(_ALIAS_PATH, source, set(), modes=("alias-rules",))
    # Only the rename module reads and compares the name.
    for site in (
        "control/src/vonk_control/profile_capacity.py",
        "control/src/vonk_control/profile_stop_authority.py",
    ):
        assert check_source(site, source, lines, modes=("alias-rules",))
    assert not check_source(
        "control/src/vonk_control/run_route_name.py",
        source,
        lines,
        modes=("alias-rules",),
    )


@pytest.mark.parametrize(
    "source",
    [
        "run.alias = alias\n",
        "run.alias += '-x'\n",
        "item.alias = alias\n",
        "setattr(row, 'alias', alias)\n",
        "stmt = insert(RecipeRun).values({'alias': alias})\n",
        "stmt = update(RecipeRun).values({'alias': alias})\n",
        "stmt = update(RecipeRun).where(RecipeRun.id == x).values(alias=alias)\n",
        "run = RecipeRun(alias=alias, plan_digest=d)\n",
    ],
)
def test_a_run_alias_written_outside_the_rename_module_is_flagged(
    source: str,
) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_ALIAS_PATH, source, lines, modes=("alias-rules",))
    assert not check_source(_ALIAS_PATH, source, set(), modes=("alias-rules",))
    assert not check_source(
        "control/src/vonk_control/run_route_name.py",
        source,
        lines,
        modes=("alias-rules",),
    )


def test_a_request_side_alias_comparison_passes() -> None:
    for source in (
        "same = assignment.alias == alias\n",
        "stale = plan.alias != stop.alias\n",
        "own = request.alias == alias\n",
    ):
        assert not check_source(_ALIAS_PATH, source, {1}, modes=("alias-rules",))


def test_a_run_is_created_with_its_name_only_in_run_admission() -> None:
    source = "run = RecipeRun(alias=alias, plan_digest=d)\n"
    assert not check_source(
        "control/src/vonk_control/run_admission.py",
        source,
        {1},
        modes=("alias-rules",),
    )
    assert check_source(
        "control/src/vonk_control/run_admission.py",
        "run.alias = alias\n",
        {1},
        modes=("alias-rules",),
    )


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
        "control/src/vonk_control/library/api.py", other, {1}, modes=("agent-422",)
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
            "control/src/vonk_control/catalog/revision_collection.py",
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
            "control/src/vonk_control/inventory/repository.py",
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
        "control/src/vonk_control/stored/json_values.py",
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
        "control/src/vonk_control/catalog/sync_contract.py",
        "control/src/vonk_control/telemetry/maintenance.py",
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

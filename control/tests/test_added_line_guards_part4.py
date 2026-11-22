import pytest
from vonk_agent_protocol import LifecycleState

from .added_line_guards import check_source
from .added_line_guards_support import PATH, _call, _docker_listings


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
            "jobs.states.words(\n    LifecycleState.QUEUED,\n"
            "    LifecycleState.RUNNING,\n    LifecycleState.NEEDS_OPERATOR,\n)"
        ),
        "{LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}",
        "(State.RUNNING, State.OBSERVING)",
        "jobs.states.means(row.state, LifecycleState.QUEUED, LifecycleState.BACKOFF)",
        "frozenset({aos.QUEUED, aos.RUNNING})",
        "('queued', 'running')",
        # An "ended" set that counted a wait for a person as ended.
        "{LifecycleState.FAILED, LifecycleState.CANCELLED, LifecycleState.NEEDS_OPERATOR}",
        "jobs.states.words(LifecycleState.SUCCEEDED, LifecycleState.FAILED)",
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
            "jobs.states.words(LifecycleState.QUEUED)"
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
        "jobs.states.words(LifecycleState.NEEDS_OPERATOR)",
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


_ASYNC_SRC = "control/src/vonk_control/example.py"


_ASYNC_REPOSITORY = (
    "class Stream:\n"
    "    def __init__(self, events: EventRepository) -> None:\n"
    "        self._events = events\n"
    "    async def run(self):\n"
    "        return {body}\n"
)


@pytest.mark.parametrize(
    "source",
    [
        "async def f(session):\n    return session.get(Row, 1)\n",
        "async def f(self):\n    with self._sessions() as session:\n        pass\n",
        "async def f(sessions):\n    with sessions.begin() as session:\n        pass\n",
        "async def f(repository):\n    return repository.list()\n",
        "async def f(path):\n    return open(path).read()\n",
        "async def f():\n    time.sleep(0.05)\n",
        "async def f(fd):\n    os.fsync(fd)\n",
        "async def f(fd):\n    fcntl.flock(fd, fcntl.LOCK_EX)\n",
        "async def f():\n    return httpx2.Client(base_url='x')\n",
        "async def f():\n    async def g():\n        time.sleep(1)\n",
        (
            "async def f(session, repository):\n"
            "    await asyncio.to_thread(repository.get, session.get(Row, 1))\n"
        ),
        _ASYNC_REPOSITORY.format(body="self._events.high_watermark()"),
        "async def f():\n    return subprocess.run(['ls'])\n",
        "async def f(path):\n    return path.read_text()\n",
        "async def f(path):\n    path.write_bytes(b'x')\n",
        "async def f(path):\n    return tempfile.mkstemp(dir=path)\n",
        "async def f(a, b):\n    os.replace(a, b)\n",
        "async def f(session):\n    return session.get(Row, 1)  # sequential: because\n",
        # Only startup/shutdown/one-off code is exempt; the sequencing reasons
        # justify an order, not a blocked event loop.
        "async def f(session):\n    return session.get(Row, 1)  # sequential: one-session\n",
        "async def f(session):\n    return session.get(Row, 1)  # sequential: ordering\n",
        # A same-module sync helper (or context manager) with I/O is I/O.
        "def load(path):\n    return path.read_text()\nasync def f(path):\n    return load(path)\n",
        "def load(path):\n    return path.read_text()\ndef mid(path):\n    return load(path)\nasync def f(path):\n    return mid(path)\n",
        (
            "class S:\n"
            "    def read(self, path):\n"
            "        return open(path).read()\n"
            "    async def f(self, path):\n"
            "        return self.read(path)\n"
        ),
        (
            "@contextmanager\n"
            "def locked(fd):\n"
            "    fcntl.flock(fd, 1)\n"
            "    yield\n"
            "async def f(fd):\n"
            "    with locked(fd):\n"
            "        pass\n"
        ),
        "async def f(session):\n    return session.get(Row, 1)  # sequential:\n",
    ],
)
def test_sync_io_inside_async_def_is_flagged(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_ASYNC_SRC, source, lines, modes=("sync-in-async",))
    # Only an added line can fail the change.
    assert not check_source(_ASYNC_SRC, source, set(), modes=("sync-in-async",))


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (
            _ASYNC_SRC,
            "async def f(session):\n    await asyncio.to_thread(session.get, Row, 1)\n",
        ),
        (_ASYNC_SRC, "async def f(r):\n    return await run_in_threadpool(r.read)\n"),
        (_ASYNC_SRC, "async def f(repository):\n    return await repository.list()\n"),
        (_ASYNC_SRC, "def f(session):\n    return session.get(Row, 1)\n"),
        (
            _ASYNC_SRC,
            "async def f(session):\n    def g():\n        return session.get(Row, 1)\n    return await asyncio.to_thread(g)\n",
        ),
        (
            _ASYNC_SRC,
            "async def f(session):\n    await asyncio.to_thread(lambda: session.get(Row, 1))\n",
        ),
        (_ASYNC_SRC, "async def f():\n    await asyncio.sleep(1)\n"),
        (
            _ASYNC_SRC,
            "async def f(session):\n    return session.get(Row, 1)  # sequential: not-hot\n",
        ),
        (
            _ASYNC_SRC,
            "def load(path):\n    return path.read_text()  # sequential: not-hot\nasync def f(path):\n    return load(path)\n",
        ),
        (
            _ASYNC_SRC,
            "def load(path):\n    return path.read_text()\nasync def f(path):\n    return await asyncio.to_thread(load, path)\n",
        ),
        (
            _ASYNC_SRC,
            "async def f(session):\n    # sequential: not-hot\n    return session.get(Row, 1)\n",
        ),
        (
            _ASYNC_SRC,
            "async def f(session):\n    return session.get(\n        Row, 1\n    )  # sequential: not-hot\n",
        ),
        (
            "control/tests/test_example.py",
            "async def f(session):\n    return session.get(Row, 1)\n",
        ),
        (
            "src/cluster_profiles/x.py",
            "async def f(session):\n    return session.get(Row, 1)\n",
        ),
    ],
)
def test_threaded_nested_marked_or_out_of_scope_io_passes(
    path: str, source: str
) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert not check_source(path, source, lines, modes=("sync-in-async",))


@pytest.mark.parametrize(
    "reason", ["data-dependency", "ordering", "one-session", "one-resource", "other"]
)
def test_only_not_hot_exempts_io_on_the_event_loop(reason: str) -> None:
    source = f"async def f(session):\n    return session.get(Row, 1)  # sequential: {reason}\n"
    assert check_source(_ASYNC_SRC, source, {1, 2}, modes=("sync-in-async",))


def test_adding_the_async_keyword_alone_carries_the_bodys_findings() -> None:
    # Turning `def f` into `async def f` adds only line 1; the body's sync I/O
    # is reported on that line.
    source = "async def f(path):\n    return path.read_text()\n"
    assert check_source(_ASYNC_SRC, source, {1}, modes=("sync-in-async",))
    # An untouched async def with a clean body stays quiet.
    assert not check_source(
        _ASYNC_SRC,
        "async def f(path):\n    return await asyncio.to_thread(path.read_text)\n",
        {1},
        modes=("sync-in-async",),
    )
    # An untouched async def does not fail for old findings.
    assert not check_source(_ASYNC_SRC, source, set(), modes=("sync-in-async",))


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
        (_GIT_TEST_PATH, 'subprocess.run(["/usr/bin/git", "init"])\n'),
        (_GIT_TEST_PATH, 'env = {"GIT_AUTHOR_NAME": "a"}\n'),
        ("tests/test_example.py", 'subprocess.run(["git", "init"])\n'),
        (
            "control/web/src/staged-code.test.ts",
            'execFileSync("git", ["init"], { cwd });\n',
        ),
        ("web/scripts/check.test.mjs", "spawnSync('git', ['add', '.']);\n"),
        ("tests/nodes/test_example.sh", 'rev="$(git rev-parse HEAD)"\n'),
        ("tests/nodes/test_example.sh", "cd repo && git init -q\n"),
        ("tests/nodes/test_example.sh", "if git diff --quiet; then echo clean; fi\n"),
        ("tests/nodes/test_example.sh", "while ! git fetch -q; do :; done\n"),
        ("tests/nodes/test_example.sh", "exec git status\n"),
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
        # A faked git is allowed: the helper is monkeypatched, nothing spawns.
        (_GIT_TEST_PATH, 'monkeypatch.setattr(module, "_git_source", fake)\n'),
        (_GIT_TEST_PATH, 'git(repo, "commit", "-m", "x")\n'),
        (_GIT_TEST_PATH, 'result = helpers._git(repo, "init")\n'),
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


FUTURE_WORKER_HEADER = """\
import threading
from concurrent.futures import Future


def start(read):
    future = Future()

"""


EXCEPTION_ONLY_WORKER = """\
    def work():
        try:
            future.set_result(read())
        except Exception as error:
            future.set_exception(error)

    threading.Thread(target=work, daemon=True).start()
    return future.result()
"""


BASE_EXCEPTION_WORKER = EXCEPTION_ONLY_WORKER.replace(
    "except Exception", "except BaseException"
)


BARE_EXCEPT_WORKER = EXCEPTION_ONLY_WORKER.replace(
    "except Exception as error", "except"
)


# An unrelated bounded wait elsewhere in the module does not exempt the worker.
TIMEOUT_WAITER = EXCEPTION_ONLY_WORKER + "    stop.wait(timeout=1)\n"


NO_COMPLETION_WORKER = """\
    def work():
        read()

    threading.Thread(target=work, daemon=True).start()
"""


def _future_worker_hits(body: str, lines: set[int] | None = None) -> list[str]:
    source = FUTURE_WORKER_HEADER + body
    added = lines or set(range(1, len(source.splitlines()) + 1))
    return check_source("src/pkg/reads.py", source, added, modes=("future-workers",))


def test_future_worker_catching_only_exception_is_flagged():
    assert _future_worker_hits(EXCEPTION_ONLY_WORKER)
    # Only an added line is reported.
    assert not _future_worker_hits(EXCEPTION_ONLY_WORKER, {1})


def test_unrelated_timeout_wait_does_not_exempt_an_exception_only_worker():
    assert _future_worker_hits(TIMEOUT_WAITER)


def test_edit_inside_an_existing_worker_is_flagged():
    source = FUTURE_WORKER_HEADER + EXCEPTION_ONLY_WORKER
    inside = source.splitlines().index("            future.set_exception(error)") + 1
    assert _future_worker_hits(EXCEPTION_ONLY_WORKER, {inside})
    # An added line after the worker is not part of it.
    assert not _future_worker_hits(EXCEPTION_ONLY_WORKER, {inside + 3})


@pytest.mark.parametrize(
    "body",
    [BASE_EXCEPTION_WORKER, BARE_EXCEPT_WORKER, NO_COMPLETION_WORKER],
)
def test_future_worker_that_cannot_hang_its_waiter_is_allowed(body):
    assert not _future_worker_hits(body)


_PAGE_CEILINGS = (
    "def keys(client):\n"
    "    for page in range(1, _MAX_PAGES + 1):\n"
    "        client.request('GET', '/key/list', page=page)\n"
    "\n"
    "\n"
    "def repair(store):\n"
    "    for _ in range(_MAX_REPAIRS):\n"
    "        store.repair()\n"
    "\n"
    "\n"
    "def hops(client):\n"
    "    for hop in range(_MAX_HTTP_REDIRECTS + 1):  # item-count-limit: RFC 9110\n"
    "        client.get('/x')\n"
    "\n"
    "\n"
    "def small(client):\n"
    "    for attempt in range(3):\n"
    "        client.request('GET', '/x')\n"
)


def test_a_page_count_ceiling_on_a_fetch_loop_is_flagged() -> None:
    path = "control/src/vonk_control/gateway/keys.py"
    assert check_source(path, _PAGE_CEILINGS, {2}, modes=("item-counts",))
    # No fetch in the body, a stated protocol limit, and a literal retry count.
    assert not check_source(path, _PAGE_CEILINGS, {7}, modes=("item-counts",))
    assert not check_source(path, _PAGE_CEILINGS, {12}, modes=("item-counts",))
    assert not check_source(path, _PAGE_CEILINGS, {17}, modes=("item-counts",))
    # Old lines stay quiet.
    assert not check_source(path, _PAGE_CEILINGS, {1, 3}, modes=("item-counts",))


_OWNER_PATH = "control/src/vonk_control/recipe_operations/example.py"


def _job_owner_payload(source: str, added: set[int]) -> list[str]:
    return check_source(_OWNER_PATH, source, added, modes=("job-owner-payload",))


@pytest.mark.parametrize("key", ["owner_kind", "owner_id"])
def test_a_job_owner_extracted_from_the_payload_in_sql_is_flagged(key: str) -> None:
    source = f'q = select(Job).where(Job.payload["{key}"].as_string() == run.id)\n'
    assert _job_owner_payload(source, {1})
    assert not _job_owner_payload(source, set())


def test_the_owner_columns_and_other_payload_keys_are_not_flagged() -> None:
    source = (
        "q = select(Job).where(Job.owner_id == run.id)\n"
        'r = select(Job).where(Job.payload["run_id"].as_string() == run.id)\n'
        'owner = job.payload.get("owner_id")\n'
    )
    assert not _job_owner_payload(source, {1, 2, 3})


_CLOSURE_PATH = "control/src/vonk_control/example.py"


_CLOSURE = ("fixture-closures",)


@pytest.mark.parametrize(
    "source",
    [
        "required = {request.fixture, *request.input_slots.values()}\n",
        "paths.add(check.request.fixture)\n",
        'fixture = getattr(request, "fixture", None)\n',
        'slots = getattr(request, "input_slots", {})\n',
        "paths.update(check.request.input_slots.values())\n",
        "paths.add(check.request.file.fixture)\n",
        'paths.add(f"{check.request.file.fixture}")\n',
        "job = check.request\npaths.add(job.fixture)\n",
    ],
)
def test_a_hand_written_fixture_closure_is_flagged(source: str) -> None:
    lines = set(range(1, source.count("\n") + 1))
    assert check_source(_CLOSURE_PATH, source, lines, modes=_CLOSURE)
    assert check_source(
        "scripts/example",
        "#!/usr/bin/env python3\n" + source,
        {line + 1 for line in lines},
        modes=_CLOSURE,
    )
    # A test harness that fakes a package closure is held to the same rule.
    assert check_source(
        "control/tests/example_fixtures.py", source, lines, modes=_CLOSURE
    )
    assert not check_source(_CLOSURE_PATH, source, set(), modes=_CLOSURE)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        # The check owns its closure.
        (_CLOSURE_PATH, "paths = set(check.fixture_paths())\n"),
        # A pytest fixture and an unrelated attribute are not a closure.
        (_CLOSURE_PATH, "@pytest.fixture\ndef value(): ...\n"),
        (_CLOSURE_PATH, "name = options.fixture\n"),
        # The executor's own typed request, not a check's.
        (_CLOSURE_PATH, "name = declared.file.fixture\n"),
        # Building a request names its fields; reading a closure out of one is the hit.
        (_CLOSURE_PATH, 'request = Job(fixture="a", input_slots={})\n'),
    ],
)
def test_fixture_closure_guard_leaves_other_lines_alone(path: str, source: str) -> None:
    assert not check_source(path, source, {1, 2}, modes=_CLOSURE)


def test_a_readiness_reader_does_not_key_on_the_asking_recipe():
    reader = "control/src/vonk_control/library_projection/local_state.py"
    column = (
        "select(ModelCacheSet).where(ModelCacheSet.recipe_revision_sha256.in_(d))\n"
    )
    assert check_source(PATH, column, {1}, modes=("identity",))
    assert not check_source(PATH, column, {2}, modes=("identity",))
    for line in (
        "stmt = select(RecipeBuild.recipe_revision_id)\n",
        "other = revisions.document_id\n",
    ):
        assert check_source(reader, line, {1}, modes=("identity",))
        assert not check_source(PATH, line, {1}, modes=("identity",))


@pytest.mark.parametrize(
    "body",
    [
        "    assert 'Spark' in capsys.readouterr().out\n",
        "    assert 'ready' in captured.out and status == 0\n",
        "    assert f'Job: {job}' in buffer.getvalue()\n",
        "    assert 'Reconnect' in captured.err.splitlines()\n",
        "    out, err = capsys.readouterr()\n    assert 'Ready' in out\n",
    ],
)
def test_a_vonkctl_test_may_not_assert_wording_in_printed_output(body: str) -> None:
    source = "def test_x(capsys):\n" + body
    path = "tests/cluster_profiles/test_example.py"
    modes = ("printed-output",)
    every_line = set(range(1, source.count("\n") + 1))
    assert check_source(path, source, {source.count("\n")}, modes=modes)
    # The layout test and other trees may.
    for allowed in ("tests/cluster_profiles/test_cli_layout.py", "tests/test_x.py"):
        assert not check_source(allowed, source, every_line, modes=modes)


@pytest.mark.parametrize(
    "body",
    [
        # Leak, duplication and emptiness checks on captured output are data.
        "    assert TOKEN not in captured.out + captured.err\n",
        "    assert capsys.readouterr().out.count(SECRET) == 1\n",
        "    assert not capsys.readouterr().out\n",
        "    output = capsys.readouterr()\n",
        "    assert 'x' in document['detail']\n",
        "    doc = json.loads(capsys.readouterr().out)\n    assert 'x' in doc\n",
    ],
)
def test_leak_and_count_checks_on_printed_output_stay_allowed(body: str) -> None:
    source = "def test_x(capsys):\n" + body
    path = "tests/cluster_profiles/test_example.py"
    assert not check_source(path, source, {1, 2, 3}, modes=("printed-output",))


_RETRY_PATH = "control/src/vonk_control/fleet_profiles/retry.py"


@pytest.mark.parametrize(
    "handler",
    [
        (
            "except (KeyError, ValueError) as error:\n"
            "    return self._decline_retry_application(app, CONFLICT, str(error))\n"
        ),
        (
            "except (\n    Busy,\n    RuntimeError,\n) as error:\n"
            "    self._defer_exact_step(row, progress, str(error), now)\n"
        ),
        "except Exception:  # internal\n    self._park_for_retry(row, progress, [])\n",
    ],
)
def test_a_broad_handler_that_parks_work_is_flagged(handler: str) -> None:
    # Wrong implementation (#2299): an internal InvalidValue during a load
    # retry was parked as a conflict and backed off for ever.
    source = "try:\n    work()\n" + handler
    assert check_source(_RETRY_PATH, source, {3}, modes=("retry-dispositions",))


@pytest.mark.parametrize(
    "handler",
    [
        (
            "except (KeyError, ValueError) as error:\n"
            "    if disposition(error) is not Disposition.FAIL:\n"
            "        return self._decline_retry_application(app, CONFLICT, str(error))\n"
            "    return self._decline_retry_application(app, DEFECT, str(error))\n"
        ),
        (
            "except ValueError as error:\n"
            "    self._defer_exact_step(row, progress, str(error), now,\n"
            "        ending=_ending(error) if is_definite(error) else ())\n"
        ),
        (
            "except FleetProfileAdmissionBusy as error:\n"
            "    self._park_for_retry(row, progress, [])\n"
        ),
        "except ValueError:\n    return None\n",
    ],
)
def test_a_classified_or_narrow_handler_may_park(handler: str) -> None:
    source = "try:\n    work()\n" + handler
    added = set(range(1, source.count("\n") + 1))
    assert not check_source(_RETRY_PATH, source, added, modes=("retry-dispositions",))


def test_an_edit_inside_an_old_broad_parking_handler_is_flagged() -> None:
    source = (
        "try:\n    work()\nexcept ValueError as error:\n"
        "    log(error)\n    self._defer_pending_application(app, str(error))\n"
    )
    assert check_source(_RETRY_PATH, source, {5}, modes=("retry-dispositions",))
    assert check_source(_RETRY_PATH, source, {4}, modes=("retry-dispositions",))
    # An added line outside the old handler leaves it alone.
    assert not check_source(_RETRY_PATH, source, {2}, modes=("retry-dispositions",))


def test_an_inline_rank_installed_test_is_flagged() -> None:
    path = "control/src/vonk_control/other.py"
    for source in (
        "ok = node.state == InstallationNodeState.INSTALLED\n",
        "q = q.where(InstallationNode.state != InstallationNodeState.INSTALLED)\n",
    ):
        assert check_source(path, source, {1}, modes=("rank-installed",))
    assert not check_source(
        path, "ok = rank_installed(node.state)\n", {1}, modes=("rank-installed",)
    )
    assert not check_source(
        "control/src/vonk_control/machine_states.py",
        "installed = state == InstallationNodeState.INSTALLED\n",
        {1},
        modes=("rank-installed",),
    )


def test_an_inline_live_reservation_test_is_flagged() -> None:
    path = "control/src/vonk_control/other.py"
    source = "q = q.where(ResourceReservation.state != ReservationState.RELEASED)\n"
    assert check_source(path, source, {1}, modes=("live-reservation",))
    assert not check_source(
        path,
        "q = q.where(reservation_live(ResourceReservation.state))\n",
        {1},
        modes=("live-reservation",),
    )
    assert not check_source(
        "control/src/vonk_control/machine_states.py",
        "return state != ReservationState.RELEASED\n",
        {1},
        modes=("live-reservation",),
    )


def test_a_newest_snapshot_window_or_grouped_max_is_flagged() -> None:
    path = "control/src/vonk_control/other.py"
    for source in (
        "f = func.row_number().over(partition_by=NodeInventorySnapshot.node_id)\n",
        "q = select(x).group_by(NodeSnapshot.node_id)\n",
        "q = select(x).group_by(NodeInventorySnapshot.node_id, other)\n",
        "q = select(x).group_by(other, NodeInventorySnapshot.node_id)\n",
        "f = row_number().over(partition_by=(NodeInventorySnapshot.node_id, x))\n",
        "q = select(x).group_by(\n    func.max(y),\n    NodeSnapshot.node_id,\n)\n",
        "q = select(func.max(NodeInventorySnapshot.observed_at))\n",
        "q = select(func.max(NodeSnapshot.observed_at))\n",
    ):
        assert check_source(path, source, {1}, modes=("newest-snapshot",))
    assert not check_source(
        path,
        "ids = NodeInventorySnapshot.id.in_(newest_snapshot_ids(nodes))\n",
        {1},
        modes=("newest-snapshot",),
    )
    assert not check_source(
        path,
        "f = over(partition_by=X, order_by=NodeSnapshot.node_id)\n",
        {1},
        modes=("newest-snapshot",),
    )
    assert not check_source(
        path,
        "q = select(NodeInventorySnapshot.node_id).group_by(x)\n"
        "r = select(y).where(NodeSnapshot.node_id == n).order_by(z)\n",
        {1, 2},
        modes=("newest-snapshot",),
    )


def test_an_added_identity_state_literal_is_refused() -> None:
    source = 'def f(row):\n    return row.state == "revoked"\n'
    assert check_source(PATH, source, {2}, modes=("vocabulary",))
    assert not check_source(PATH, source, {1}, modes=("vocabulary",))


def test_a_broad_catch_may_not_turn_a_verdict_into_a_wait():
    from tests.legacy_tolerance import scan_source

    wait = (
        "try:\n    f()\n"
        "except Exception as error:\n    raise EvidenceUnknown('x') from error\n"
    )
    verdict_first = (
        "try:\n    f()\n"
        "except (UnknownOutcomeError, InvalidRequestError):\n    raise\n"
        "except Exception as error:\n    raise EvidenceUnknown('x') from error\n"
    )
    assert (3, "deterministic error becomes a wait") in scan_source(wait)
    assert not [
        hit
        for hit in scan_source(verdict_first)
        if "verdict" in hit[1] or "wait" in hit[1]
    ]


def test_compiled_plan_validation_is_refused_outside_its_owners():
    source = "fn f() { spec.validate()?; }\n"
    base = "rust/crates/vonk-agent/src/"
    assert check_source(
        base + "oci/lifecycle.rs", source, {1}, modes=("plan-validation",)
    )
    assert check_source(
        "rust/crates/vonk-agent-protocol/src/compiled_oci/projection.rs",
        source,
        {1},
        modes=("plan-validation",),
    )
    split = "fn f() {\n    request\n        .compiled_execution_plan\n        .validate()?;\n}\n"
    assert check_source(
        base + "oci/lifecycle.rs", split, {4}, modes=("plan-validation",)
    )
    assert not check_source(
        base + "executor/plans.rs", source, {1}, modes=("plan-validation",)
    )


def test_vonkctl_failure_needs_its_own_typed_error():
    source = 'def f():\n    raise ControlClientError("local file broke")\n'
    cli_path = "src/cluster_profiles/cli_artifact_jobs.py"
    assert check_source(cli_path, source, {2}, modes=("boundaries",))
    assert not check_source(
        "src/cluster_profiles/control_client/errors.py",
        source,
        {2},
        modes=("boundaries",),
    )


@pytest.mark.parametrize(
    ("source", "rejected"),
    [
        (
            "try:\n    f()\nexcept RecipeImageAvailabilityUnknown as e:\n    s = LifecycleState.FAILED\n",
            True,
        ),
        (
            "try:\n    f()\nexcept UnknownOutcomeError:\n    return Failure(retryable=False)\n",
            True,
        ),
        ("try:\n    f()\nexcept RecipeImageAvailabilityUnknown:\n    raise\n", False),
        ("try:\n    f()\nexcept ValueError:\n    s = LifecycleState.FAILED\n", False),
    ],
)
def test_unknown_outcome_handlers_cannot_build_terminal_values(source, rejected):
    """Catches an Unknown handler fabricating FAILED / retryable=False."""
    lines = set(range(1, len(source.splitlines()) + 1))
    hits = check_source(PATH, source, lines, modes=("waits",))
    assert bool([h for h in hits if "unknown outcome turned terminal" in h]) is rejected


_BIND_PATH = "rust/crates/vonk-agent/src/health.rs"


_BIND_SRC = (
    "#[cfg(test)]\nmod tests {\n"
    '    fn a() { let l = TcpListener::bind("127.0.0.1:0").unwrap(); }\n'
    '    fn b() { let l = TcpListener::bind(("127.0.0.1", 0)).await.unwrap(); }\n'
    "    fn c() { let l = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap(); }\n"
    "    fn d() { let l = TcpListener::bind(FIXTURE_BIND).unwrap(); }\n"
    "    fn e() { let l = UnixListener::bind(&path).unwrap(); }\n"
    "}\n"
)


def test_a_test_fixture_bound_on_127_0_0_1_is_rejected_but_the_test_address_and_unix_are_not():
    hits = check_source(
        _BIND_PATH, _BIND_SRC, _all_lines(_BIND_SRC), modes=("loopback-bind",)
    )
    assert [h.split(":")[1] for h in hits] == ["3", "4", "5"]


SHIM = (
    "#!/bin/sh\nset -e\n"
    'exec /usr/lib/vonk-forge/vonk-agent-helper --package-hook postinst "$@"\n'
)


@pytest.mark.parametrize(
    "path,source,flagged",
    [
        ("packaging/debian/postinst", SHIM, False),
        ("packaging/debian/postinst", SHIM + "install -d /var/lib/x\n", True),
        ("packaging/debian/prerm", '[ "$1" = remove ] || exit 0\n', False),
        (
            "packaging/debian/prerm",
            "/usr/lib/vonk-forge/vonk-agent-helper --package-hook prerm remove || true\n",
            False,
        ),
        (
            "packaging/debian/postrm",
            'deb-systemd-helper purge "$unit" >/dev/null || true\n',
            False,
        ),
        ("packaging/debian/postrm", "rm -rf -- /var/lib/vonk-forge-agent\n", False),
        ("packaging/debian/postrm", "rm -rf -- $dir\n", True),
        ("packaging/debian/preinst", "#!/bin/sh\n", True),
        ("packaging/debian/preinst-repair", "#!/bin/sh\n", True),
        ("packaging/debian/control", "Package: x\n", False),
        ("packaging/systemd/new.service", "ExecStart=/bin/sh -c 'x'\n", True),
        ("packaging/systemd/new.service", "ExecStart=/usr/bin/x\n", False),
    ],
)
def test_maintainer_scripts_stay_shims_and_units_run_no_shell(path, source, flagged):
    lines = set(range(1, len(source.splitlines()) + 1))
    assert bool(check_source(path, source, lines, modes=("maintainershim",))) is flagged
    # Only an added line can fail: an unchanged line of the same file is clean.
    if "preinst" not in path:
        assert not check_source(
            path, source, {len(lines) + 5}, modes=("maintainershim",)
        )

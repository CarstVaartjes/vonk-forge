"""Each fixture names a violation and its principle-preserving counterpart."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from .principle_guards import (
    ALLOWLISTS,
    ROOT,
    Site,
    baseline_history,
    evaluate_gate,
    history_gate,
    load_allowlist,
    lower,
    scan_retention,
    scan_rust,
    scan_rust_remedies,
    scan_shell,
    scan_sites,
    scan_source,
)


@pytest.mark.parametrize(
    "mode,bad,good",
    [
        (
            "waits",
            "while True:\n    sleep(1)",
            "while monotonic() < deadline:\n    sleep(1)",
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


def test_rust_poll_loop_requires_bound_in_its_own_body():
    assert scan_rust("fn serve() { loop { sleep(delay); } }", path="agent.rs")
    assert not scan_rust(
        "fn serve() { loop { if now > deadline { break; } } }", path="agent.rs"
    )
    assert scan_rust('fn serve() { loop { log!("deadline"); } }', path="agent.rs")


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


def test_ratchet_rejects_new_stale_and_moved_sites_and_lower_cannot_raise():
    site = Site("a.py", "f", "wait", 3)
    doc = {
        "schema": 1,
        "debt": [{"path": "a.py", "function": "f", "kind": "wait", "count": 1}],
        "exceptions": [],
    }
    assert not evaluate_gate([site], doc)
    assert not evaluate_gate([Site("a.py", "f", "wait", 99)], doc)
    assert evaluate_gate([site, site], doc)
    assert evaluate_gate([], doc)
    assert evaluate_gate([Site("a.py", "g", "wait", 3)], doc)
    with pytest.raises(ValueError, match="cannot increase"):
        lower(doc, [site, site])
    assert lower(doc, [])["debt"] == []


def test_exception_requires_fixed_reason_and_duplicate_keys_fail(tmp_path: Path):
    entry = {
        "path": "a.py",
        "function": "f",
        "kind": "read",
        "count": 1,
        "reason": "sounds fine",
        "justification": "fixture security rejection",
    }
    doc = {"schema": 1, "debt": [], "exceptions": [entry]}
    path = tmp_path / "allow.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="fixed reason"):
        load_allowlist(path)
    entry["reason"] = "security-edge"
    path.write_text(json.dumps(doc))
    assert load_allowlist(path)["exceptions"]
    doc["debt"] = [entry]
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="duplicate"):
        load_allowlist(path)


@pytest.fixture(scope="session", params=list(ALLOWLISTS))
def principle_inventory(request):
    # Parsing/inventory is shared setup, not charged to an individual test.
    return request.param, scan_sites(request.param)


def test_principle_debt_only_falls(principle_inventory):
    mode, sites = principle_inventory
    path = ROOT / "tools" / (ALLOWLISTS[mode] + "-allowlist.json")
    document = load_allowlist(path)
    assert evaluate_gate(sites, document) + baseline_history(path, document) == []


def test_allowlist_edits_cannot_raise_or_add_debt():
    entry = {"path": "a", "function": "f", "kind": "k", "count": 1}
    old = {"debt": [entry], "exceptions": []}
    assert not history_gate(old, old)
    assert history_gate({"debt": [{**entry, "count": 2}], "exceptions": []}, old)
    assert history_gate({"debt": [{**entry, "function": "g"}], "exceptions": []}, old)


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


def test_builtin_raise_inventory_is_report_only_and_excludes_custom_classes():
    assert scan_source(
        "raise RuntimeError('lost response')", path="owner.py", mode="raises"
    )
    assert scan_source("raise HTTPException(503)", path="owner.py", mode="raises")
    assert not scan_source(
        "raise PermissionDenied('revoked')", path="owner.py", mode="raises"
    )


def test_resource_read_refusal_requires_canonical_owner_handler():
    """A resource exception cannot hide a generic damaged-row refusal."""
    source = (
        "from .operation_api import OperationResponseTooLarge as TooLarge\n"
        "@router.get('/operations')\n"
        "def observe():\n"
        "    try:\n        project()\n"
        "    except TooLarge:\n        raise HTTPException(status_code=503)\n"
    )
    sites = scan_source(source, path="api.py", mode="reads")
    assert [site.kind for site in sites] == ["get-resource-refusal"]
    for changed in (
        source.replace(".operation_api", ".unrelated"),
        source.replace("except TooLarge:", "except ValueError:"),
        source.replace("@router", "class TooLarge(Exception): pass\n@router"),
    ):
        assert [
            site.kind for site in scan_source(changed, path="api.py", mode="reads")
        ] == ["get-refusal"]
    doc = {
        "schema": 1,
        "debt": [],
        "exceptions": [
            {
                "path": "api.py",
                "function": "observe",
                "kind": "get-resource-refusal",
                "count": 1,
                "reason": "resource-bound",
                "justification": "Exact owning reader byte allocation after optional projections are exhausted.",
            }
        ],
    }
    assert not evaluate_gate(sites, doc)
    assert evaluate_gate(
        scan_source(
            source.replace("except TooLarge:", "except ValueError:"),
            path="api.py",
            mode="reads",
        ),
        doc,
    )

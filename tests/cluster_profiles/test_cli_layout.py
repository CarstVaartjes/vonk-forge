"""vonkctl tests that read printed output for layout, counting and ordering.

Every other test decides on data: the exit status, the requests sent, a typed
result, or the ``--json`` document. The cases here check what the printed
output decides (counts, folding, ordering, what only ``--wide`` shows), never
the wording. The ``printed-output`` added-line guard keeps wording assertions
on captured output out of every other test in ``tests/cluster_profiles``.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cluster_profiles.cli_render import library as library_render
from cluster_profiles.cli_render.activity import _activity
from cluster_profiles.cli_render.administration import _catalog_sync
from cluster_profiles.cli_render.artifact_jobs import _artifact_job
from cluster_profiles.cli_render.common import _utc
from cluster_profiles.cli_render.dispatch import render_payload
from cluster_profiles.cli_render.library import _library_item, _model_counts
from cluster_profiles.cli_render.profile import _key_expired, _profile

_FIXTURES = Path(__file__).parents[1] / "fixtures"


def _vector(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text())


@pytest.fixture
def printed(capsys) -> Callable[..., list[str]]:
    """Run a renderer and return the lines it printed."""

    def run(render: Callable[..., object], *args: object, **kwargs: object):
        capsys.readouterr()
        render(*args, **kwargs)
        return capsys.readouterr().out.splitlines()

    return run


# Fleet: host checks print after the warnings, in the Controller's order.


def test_fleet_detail_prints_every_host_check_after_the_warnings(printed):
    # Catches dropping a check or the checks of an older Controller (none).
    vector = _vector("fleet-wording-host-checks.json")
    cases = vector["cases"]
    node = {
        "id": "spk_00000000000000000000000000000001",
        "display_name": "Alpha",
        "lifecycle": "ready",
        "connection": {"online_state": "online", "last_seen_at": None},
        "inventory": None,
        "telemetry": None,
        "loaded": [],
        "installed": [],
        "warnings": [],
    }
    lines = printed(render_payload, node, "fleet", action="detail")
    shown = printed(
        render_payload,
        node
        | {
            "host_checks": [case["advice"] for case in cases],
            "host_checks_note": vector["note"],
        },
        "fleet",
        action="detail",
    )
    assert shown[: len(lines)] == lines
    added = shown[len(lines) :]
    assert len([line for line in added if line.startswith("Host check: ")]) == len(
        cases
    )
    # The commands and undo lines are what the operator copies: the Controller's order.
    expected = [
        command
        for case in cases
        for command in (*case["advice"]["commands"], *case["advice"]["undo"])
    ]
    copied = [line.strip() for line in added if line.strip() in set(expected)]
    assert copied == expected


# Library: model counts and the vocabulary-less recipe page.


_MODEL_COUNT = _vector("library-model-count.json")


def _model_rows(case):
    return [
        {
            "identity": {
                "publisher": "p",
                "slug": model["id"],
                "content_sha256": "0" * 64,
            },
            # Real responses always carry model_display and omit lineage when
            # the model has none (helpers, drafters, encoders).
            "model_display": {}
            if model["lineage"] is None
            else {"lineage": model["lineage"]},
        }
        for model in case["models"]
    ]


@pytest.mark.parametrize("case", _MODEL_COUNT["cases"], ids=lambda c: c["name"])
def test_model_count_folds_variants_like_the_web(case):
    assert _model_counts(_model_rows(case)) == (
        case["models_count"],
        case["variants_count"],
    )


def test_model_library_prints_models_and_variants(printed, monkeypatch):
    case = _MODEL_COUNT["cases"][0]
    monkeypatch.setattr(library_render, "_library_row", lambda item, noun: [])
    monkeypatch.setattr(library_render, "_table", lambda labels, rows: None)
    rows = [row | {"local": {}} for row in _model_rows(case)]
    payload = {"models": rows, "next_cursor": None}
    lines = printed(library_render._library, payload, "model", detail=False, wide=False)
    assert (
        f"Models: {case['models_count']} ({case['variants_count']} variants)" in lines
    )


def test_without_the_vocabulary_the_names_show_and_no_meaning(printed):
    recipe = _vector("library-list.json")["recipes"][0]
    lines = printed(_library_item, recipe | {"usage": ["chat"]}, "recipe", detail=True)
    assert any("chat" in line for line in lines)
    assert not any(line.startswith("Meaning:") for line in lines)


def test_unused_removal_unknown_acceptance_reads_unknown_not_refused(printed):
    base = {"selector": "a", "request_key": "k", "failure": "timed out"}
    payload = {
        "unused_removal": True,
        "removals": [],
        "retained": [],
        "unreviewed": [],
        "totals": {"nas_bytes": 0, "sparks": {}},
        "submissions": [
            base | {"reconnect": "vonkctl model remove a --fleet --yes"},
            base | {"selector": "b"},
        ],
    }
    lines = printed(render_payload, payload, "model", action="remove")
    labels = [line.split(":")[0].strip() for line in lines]
    assert "Unknown" in labels and "Refused" in labels


def _stopped_fleet(titles: list[str]) -> dict[str, object]:
    node = {
        "id": "spk_1",
        "display_name": "Spark One",
        "lifecycle": "ready",
        "connection": {"online_state": "online", "last_seen_at": None},
        "inventory": None,
        "telemetry": None,
        "loaded": [],
        "warnings": [],
        "installed": [
            {"installation_id": f"inst-{index}", "title": title}
            for index, title in enumerate(titles)
        ],
    }
    return {"nodes": [node]}


def test_fleet_groups_identical_installs_and_caps_the_stopped_list(printed):
    titles = ["Twin", "Twin"] + [f"Recipe {index:02d}" for index in range(12)]
    payload = _stopped_fleet(titles)
    lines = printed(render_payload, payload, "fleet")
    stopped = lines[lines.index("Installed, not running") + 1 :]
    assert stopped[0] == "  Recipe 00 on Spark One"
    assert "  Twin on Spark One (x2)" not in stopped  # beyond the cap
    assert len([line for line in stopped if line.startswith("  Recipe")]) == 10
    assert stopped[10] == "  +4 more; `vonkctl fleet --wide` lists every installation"
    wide = printed(render_payload, payload, "fleet", wide=True)
    assert sum(line.startswith("  Twin on") for line in wide) == 2
    assert not any("+4 more" in line for line in wide)
    twin = printed(render_payload, _stopped_fleet(["Twin", "Twin"]), "fleet")
    assert twin.count("  Twin on Spark One (x2)") == 1


# Records, pages and endpoints: what is short, what only --wide shows.


def _job_record(**changes: object) -> dict[str, object]:
    sha = "a" * 64
    declaration = {
        "name": "p.txt",
        "slot": "prompt",
        "media_type": "text/plain",
        "size_bytes": 3,
        "sha256": sha,
    }
    return {
        "id": "job-1",
        "run_id": "run-1",
        "state": "succeeded",
        "interface": "image-job",
        "compiled_contract": None,
        "output_limits": None,
        "timeout_seconds": 60,
        "contract_sha256": sha,
        "input_manifest_sha256": sha,
        "output_manifest_sha256": sha,
        "input_total_bytes": 3,
        "created_at": "2026-10-02T14:22:58Z",
        "updated_at": "2026-10-02T14:23:58Z",
        "input_declarations": [declaration],
        "input_files": [declaration],
        "output_files": [
            {"name": "o.png", "media_type": "image/png", "size_bytes": 1, "sha256": sha}
        ],
        "operation_id": "op-1",
        "submit_request_id": "req-1",
    } | changes


def test_job_record_is_short_and_hashes_need_wide(printed):
    short = printed(_artifact_job, _job_record(), "detail")
    wide = printed(
        render_payload,
        _job_record(),
        "job",
        artifact_job_action="detail",
        wide=True,
    )
    assert len(short) <= 8
    assert not [line for line in short if "SHA-256" in line]
    assert len([line for line in wide if "SHA-256" in line]) == 5


def test_unfinished_job_record_stays_short(printed):
    running = _job_record(
        state="running",
        output_files=[],
        status_reason="queued",
        cancel_requested_at="2026-10-02T14:24:00Z",
    )
    assert len(printed(_artifact_job, running, "detail")) <= 8


def test_job_run_wide_adds_details_without_an_observation_line(printed):
    record = _job_record()
    wide = printed(
        render_payload,
        record,
        "job",
        action="run",
        artifact_job_action="run",
        wide=True,
    )
    assert not [line for line in wide if line.startswith("Observation")]
    assert [line for line in wide if "SHA-256" in line]


def test_library_page_hint_hides_the_cursor_until_wide(printed):
    payload = {"recipes": [], "next_cursor": "c" * 400}
    short = printed(render_payload, payload, "recipe", action="library")
    wide = printed(render_payload, payload, "recipe", action="library", wide=True)
    assert [line for line in short if line.startswith("Next page: add --cursor")]
    assert not [line for line in short if "c" * 400 in line]
    assert [line for line in wide if "c" * 400 in line]


def test_activity_blocker_prints_in_order_on_stdout(capsys):
    operation = {
        "id": "op-1",
        "kind": "profile-load",
        "state": "failed",
        "created_at": "2026-10-02T14:22:58Z",
        "node_ids": [],
        "failure": {"code": "x.failed", "detail": "no room"},
        "recovery": {"actions": []},
    }
    capsys.readouterr()
    _activity({"operations": [operation], "total": 1, "next_cursor": None}, None)
    out, err = capsys.readouterr()
    assert err == ""
    lines = out.splitlines()
    blocker = lines.index("Blocker: x.failed: no room")
    assert lines[0].startswith("Activity") and blocker > 0


def test_profile_names_its_sparks(printed):
    payload = {
        "number": 1,
        "name": "p",
        "definition": {
            "assignments": [{"recipe_selector": "r", "spark_ids": ["spk_1", "spk_2"]}]
        },
        "assignments": [],
        "spark_names": {"spk_1": "spark-a"},
    }
    lines = printed(_profile, payload)
    assert "Sparks: spark-a, spk_2" in " ".join(lines).replace("  ", " ")


def test_sync_status_counts_documents_not_recipes(printed):
    payload = {
        "state": "succeeded",
        "processed_count": 725,
        "total_count": 725,
        "problems": [],
        "stale_recipes": [],
        "withdrawn_recipes": [],
    }
    lines = printed(_catalog_sync, payload)
    assert [line for line in lines if line.startswith("Documents: 725 of 725")]
    assert not [line for line in lines if line.startswith("Recipes")]


# Times, endpoint detail and expired keys.

_ENDPOINT = {
    "api_base": "https://vonk-forge.example.ts.net/v1",
    "backend_api_base": "http://10.0.0.10:8000/v1",
    "alias": "coder",
    "generation": 8,
    "node_id": "spk_" + "a" * 32,
    "plan_digest": "a" * 64,
    "observed_at": "2026-09-23T12:59:30.611000+02:00",
}


def _endpoint_payload(**changes):
    return {
        "number": 3,
        "application_id": "load-1",
        "application_state": "succeeded",
        "observed_at": "2026-09-23T12:59:31Z",
        "assignments": [
            {
                "assignment_id": "33333333-3333-4333-8333-000000000001",
                "desired_state": "running",
                "alias": "coder",
                "recipe_title": "Qwen Code",
                "state": "published",
                "endpoint": _ENDPOINT,
            }
        ],
        **changes,
    }


def test_profile_endpoint_is_short_until_wide(printed):
    short = printed(render_payload, _endpoint_payload(), "profile", action="endpoint")
    wide = printed(
        render_payload, _endpoint_payload(), "profile", action="endpoint", wide=True
    )
    route = ("Route generation", "Route observed at", "Freshness", "Spark backend")
    assert [t for t in short if t.startswith(route)] == []
    assert "API base (inference gateway): https://vonk-forge.example.ts.net/v1" in short
    assert "curl example:" in short
    assert [t for t in short if t.startswith("Python example")] == []
    assert "Route observed at: 2026-09-23 10:59:30Z" in wide
    assert "Spark backend (diagnostic): http://10.0.0.10:8000/v1" in wide
    assert "Python example (openai client):" in wide


def test_wide_endpoint_with_invalid_history_still_answers(printed):
    payload = _endpoint_payload(
        projection_issue={
            "code": "profile.application_intent.invalid",
            "detail": "stored load history is invalid",
        }
    )
    lines = printed(render_payload, payload, "profile", action="endpoint", wide=True)
    assert "Stored evidence: stored load history is invalid" in lines


def test_utc_time_literal():
    assert _utc(datetime(2026, 10, 2, 16, 22, 58, 611000, tzinfo=UTC)) == (
        "2026-10-02 16:22:58Z"
    )
    assert _utc(datetime.fromisoformat("2026-10-02T16:22:58+02:00")) == (
        "2026-10-02 14:22:58Z"
    )


@pytest.mark.parametrize(
    ("expires", "expired"),
    [
        ("2026-10-02T14:22:58+00:00", True),
        ("2026-10-02T14:22:59+00:00", False),
        ("2027-01-01T00:00:00Z", False),
        ("not a time", False),
    ],
)
def test_key_expiry_is_decided_against_now(expires, expired):
    now = datetime(2026, 10, 2, 14, 22, 58, tzinfo=UTC)
    assert _key_expired(expires, now) is expired


def test_one_time_formatter_owns_isoformat():
    render = Path(__file__).parents[2] / "src/cluster_profiles/cli_render"
    users = [
        path.name
        for path in render.glob("*.py")
        if ".isoformat(" in path.read_text() and path.name != "common.py"
    ]
    assert users == []

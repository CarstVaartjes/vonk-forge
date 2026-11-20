"""The one place a vonkctl test reads printed text.

Every other test decides on data: the exit status, the requests sent, a typed
result, or the ``--json`` document. Human wording is owned by the shared
vectors in ``tests/fixtures`` that the Controller web renders too, so each case
here renders a vector's input and compares the exact text vonkctl prints. The
``printed-output`` added-line guard keeps wording assertions on captured output
out of every other test in ``tests/cluster_profiles``.
"""

import argparse
import json
import re
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from library_route_fixtures import _library_detail, _recipe_projection
from library_route_fixtures import _recipe as _recipe_document

from cluster_profiles.cli import _parser
from cluster_profiles.cli_render import (
    _activity,
    _artifact_job,
    _catalog_sync,
    _library_item,
    render_payload,
)
from cluster_profiles.cli_render import library as library_render
from cluster_profiles.cli_render.common import _utc
from cluster_profiles.cli_render.host_checks import host_check_lines
from cluster_profiles.cli_render.library import _model_counts
from cluster_profiles.cli_render.metrics import metric_value_text
from cluster_profiles.cli_render.profile import (
    _connection_examples,
    _key_expired,
    _profile,
)
from cluster_profiles.cli_render.throttle import throttle_words
from cluster_profiles.cli_render.vocabulary import use_vocabulary
from cluster_profiles.generated_control.models.catalog_vocabulary import (
    CatalogVocabulary,
)
from cluster_profiles.generated_control.models.host_check_advice import (
    HostCheckAdvice,
)

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


def _cells(lines: list[str], offset: int = 0) -> dict[str, str]:
    header = next(line for line in lines if line.startswith("SPARK"))
    row = lines[lines.index(header) + 1 + offset]
    return dict(zip(re.split(r"\s{2,}", header), re.split(r"\s{2,}", row), strict=True))


# Fleet: the status line, attention items, vitals, Serving and throttle words.

_STATUS = _vector("fleet-wording-status.json")["cases"]


@pytest.mark.parametrize("case", _STATUS, ids=lambda case: case["name"])
def test_fleet_status_line_is_the_vector_line(case, printed):
    payload = {
        "nodes": case["nodes"],
        "attention": case.get("attention", []),
        "attention_unavailable": case.get("attention_unavailable", False),
    }
    assert printed(render_payload, payload, "fleet")[0] == case["expected"]


_ATTENTION = _vector("fleet-wording-attention.json")
_ALERTS_YAML = (
    _FIXTURES.parents[1] / "deploy" / "compose" / "prometheus" / "alerts.yaml"
)


def _needs_attention(printed, **fleet) -> list[str]:
    nodes = [
        {
            **node,
            "connection": {"online_state": "online", "last_seen_at": None},
            "inventory": None,
            "telemetry": None,
            "loaded": [],
            "installed": [],
            "warnings": [],
        }
        for node in _ATTENTION["nodes"]
    ]
    lines = printed(render_payload, {"nodes": nodes, **fleet}, "fleet")
    start = lines.index("Needs attention") + 1
    return [line.strip() for line in lines[start : lines.index("", start)]]


def test_fleet_alerts_are_the_vector_lines(printed):
    assert (
        _needs_attention(printed, attention=_ATTENTION["alerts"])
        == _ATTENTION["expected"]
    )


def test_fleet_unreadable_alerts_are_the_vector_sentence(printed):
    lines = _needs_attention(printed, attention=[], attention_unavailable=True)
    assert lines == [_ATTENTION["unavailable"]]


def test_vector_alerts_carry_the_summaries_of_the_shipped_rules():
    rules = {
        rule["alert"]: rule["annotations"]["summary"]
        for group in json.loads(_ALERTS_YAML.read_text())["groups"]
        for rule in group["rules"]
    }
    shipped = [alert for alert in _ATTENTION["alerts"] if alert["name"] in rules]
    assert shipped
    assert all(alert["summary"] == rules[alert["name"]] for alert in shipped)


@pytest.mark.parametrize(
    "case", _vector("fleet-wording-vitals.json")["cases"], ids=lambda c: c["name"]
)
def test_fleet_row_vitals_are_the_vector_words(case, printed):
    node = {
        "id": f"spk_{1:032x}",
        "display_name": "Alpha",
        "lifecycle": "ready",
        "connection": {"online_state": "online", "last_seen_at": None},
        "loaded": [],
        "installed": [],
        "warnings": [],
        **case["node"],
    }
    cells = _cells(printed(render_payload, {"nodes": [node]}, "fleet"))
    expected = case["expected"]
    assert cells["MEMORY USED"] == expected["memory"]
    assert cells["DISK FREE"] == expected["disk"]
    assert cells["GPU"] == expected["gpu"]


_WORKLOADS = _vector("fleet-wording-workloads.json")


def test_fleet_row_serving_is_the_vector_words(printed):
    nodes = [
        {
            "lifecycle": "ready",
            "connection": {"online_state": "online", "last_seen_at": None},
            "inventory": None,
            "telemetry": None,
            "installed": [],
            "warnings": [],
            **node,
        }
        for node in _WORKLOADS["nodes"]
    ]
    lines = printed(render_payload, {"nodes": nodes}, "fleet")
    for offset, node in enumerate(nodes):
        expected = _WORKLOADS["expected"][node["id"]]["vonkctl"]
        assert _cells(lines, offset)["SERVING"] == expected


@pytest.mark.parametrize(
    "case",
    _vector("fleet-wording-throttle.json")["cases"],
    ids=lambda case: str(case["reasons"]),
)
def test_throttle_reason_mask_reads_the_vector_words(case):
    assert throttle_words(case["reasons"]) == case["words"]


@pytest.mark.parametrize(
    "case",
    _vector("fleet-wording-host-checks.json")["cases"],
    ids=lambda case: case["advice"]["check"],
)
def test_host_checks_read_the_vector_lines(case):
    assert host_check_lines(HostCheckAdvice.from_dict(case["advice"])) == case["lines"]


def test_fleet_detail_prints_every_host_check_after_the_warnings(printed):
    # Catches dropping a command or the checks of an older Controller (none).
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
    expected = [f"Host checks: {vector['note']}"] + [
        line
        for case in cases
        for line in [f"Host check: {case['lines'][0]}"]
        + [f"  {command}" for command in case["lines"][1:]]
    ]
    assert shown == lines + expected


# Library: the published vocabulary, recipe links and version lines.

_WORDING = _vector("library-vocabulary-wording.json")
_RECIPE = _vector("library-list.json")["recipes"][0]


_MODEL_COUNT = _vector("library-model-count.json")


def _model_rows(case):
    return [
        {
            "identity": {
                "publisher": "p",
                "slug": model["id"],
                "content_sha256": "0" * 64,
            },
            "model_display": None
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


@pytest.fixture
def published_words():
    use_vocabulary(CatalogVocabulary.from_dict(_WORDING["vocabulary"]))
    yield
    use_vocabulary(None)


def _recipe_lines(printed, **changes) -> list[str]:
    return printed(_library_item, _RECIPE | changes, "recipe", detail=True)


@pytest.mark.parametrize("case", _WORDING["capabilities"], ids=lambda c: c["name"])
def test_usage_line_is_the_vector_line(case, printed, published_words):
    assert case["vonkctl"] in _recipe_lines(printed, usage=case["usage"])


@pytest.mark.parametrize("case", _WORDING["statuses"], ids=lambda c: c["status"])
def test_status_meaning_line_is_the_vector_line(case, printed, published_words):
    qualification = _RECIPE["qualification"] | {"status": case["status"]}
    assert case["vonkctl"] in _recipe_lines(printed, qualification=qualification)


def test_without_the_vocabulary_the_names_show_and_no_meaning(printed):
    lines = _recipe_lines(printed, usage=["chat"])
    assert "Capabilities: chat" in lines
    assert not any(line.startswith("Meaning:") for line in lines)


@pytest.mark.parametrize(
    "case", _vector("recipe-links.json")["cases"], ids=lambda c: c["name"]
)
def test_recipe_detail_links_are_the_vector_lines(case, printed):
    corpus = json.loads(
        (
            Path(__file__).parents[2]
            / "control/web/src/test-fixtures/library-corpus.json"
        ).read_text()
    )
    display = next(iter(corpus["seedDisplays"]["models"].values()))
    document = cast(dict[str, Any], _recipe_document(case["slug"]))
    document["identity"]["publisher"] = case["publisher"]
    document["provenance"]["source_reference"] = case["source_reference"]
    models = cast(dict[str, Any], _library_detail(document)["detail"])[
        "model_documents"
    ][:1]
    models = [
        {**models[0], "model_display": {**display, **item}} for item in case["models"]
    ]
    lines = printed(
        render_payload,
        {
            **_recipe_projection(f"{case['publisher']}/{case['slug']}", "Tiny"),
            "document": document,
            "model_documents": models,
        },
        "recipe",
        action="detail",
    )
    assert [line for line in lines if line in case["lines"]] == case["lines"]


@pytest.mark.parametrize(
    "case", _vector("version-line.json")["cases"], ids=lambda c: c["name"]
)
def test_version_lines_are_the_vector_lines(case, printed):
    platform = {
        "observed_at": "2026-11-12T08:00:00Z",
        "api": {
            "source_sha": case["platform"]["source_sha"],
            "control_contract_sha256": "b" * 64,
        },
        "workers": None,
        "worker_issue": "worker-observation-unavailable",
    }
    lines = printed(render_payload, platform, "platform") + printed(
        render_payload,
        case["sync"] or {"state": "never-run"},
        "recipe",
        action="sync-status",
    )
    for line in case["lines"]:
        assert line in lines


# Serving: what a profile serves and how a client connects to it.

_SERVING_NOW = _vector("serving-now.json")


@pytest.mark.parametrize("case", _SERVING_NOW["cases"], ids=lambda c: c["name"])
def test_profile_endpoint_serving_lines_are_the_vector_lines(case, printed):
    assignments = [
        {
            "assignment_id": f"33333333-3333-4333-8333-{index:012d}",
            "desired_state": "running",
            **item,
            "endpoint": {
                "alias": item["alias"],
                "api_base": "https://vonk-forge.example.ts.net/v1",
                "backend_api_base": "http://10.0.0.10:8000/v1",
                "generation": 8,
                "node_id": "spk_" + "a" * 32,
                "observed_at": "2026-09-23T12:59:30Z",
                "plan_digest": "a" * 64,
            }
            if item["state"] == "published"
            else None,
        }
        for index, item in enumerate(case["assignments"])
    ]
    lines = printed(
        render_payload,
        {
            "number": 3,
            "profile_id": "11111111-1111-4111-8111-111111111111",
            "application_id": "22222222-2222-4222-8222-222222222222",
            "application_state": "succeeded",
            "observed_at": "2026-09-23T12:59:31Z",
            "assignments": assignments,
        },
        "profile",
        action="endpoint",
    )
    start = lines.index(_SERVING_NOW["heading"])
    assert lines[start + 1 : start + 1 + len(case["lines"])] == case["lines"]
    assert lines[start + 1 + len(case["lines"])] == ""


_CONNECTION = _vector("connection-examples.json")


@pytest.mark.parametrize("case", _CONNECTION["cases"], ids=lambda c: c["alias"])
def test_connection_examples_are_the_vector_text(case, printed):
    out = "\n".join(
        printed(_connection_examples, case["api_base"], case["alias"], wide=True)
    )
    for name in ("setup", "curl", "python"):
        assert textwrap.indent(case[name], "  ") in out
    assert _CONNECTION["note"] in out


# Metrics: the shared number wording is a pure function of unit and value.


@pytest.mark.parametrize(
    "case",
    _vector("metrics-wording.json")["cases"],
    ids=lambda c: f"{c['unit']}-{c['value']}",
)
def test_metric_values_read_as_the_vector_wording(case):
    assert metric_value_text(case["unit"], case["value"]) == case["text"]


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
    unknown = next(line for line in lines if line.startswith("Unknown"))
    assert "reconnect: vonkctl model remove a --fleet --yes" in unknown


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


def test_fleet_workload_route_is_plain_words(printed):
    node = _WORKLOADS["nodes"][0]
    payload = _stopped_fleet([])
    cast(list[dict[str, Any]], payload["nodes"])[0]["loaded"] = [
        {**node["loaded"][0], "route_state": "withdrawn", "group_state": "ready"}
    ]
    text = "\n".join(printed(render_payload, payload, "fleet"))
    assert "not taking requests" in text and "withdrawn" not in text


def test_fleet_failed_route_names_the_publication_outcome(printed):
    node = _WORKLOADS["nodes"][0]
    payload = _stopped_fleet([])
    cast(list[dict[str, Any]], payload["nodes"])[0]["loaded"] = [
        {**node["loaded"][0], "route_state": "failed"}
    ]
    assert "route publication failed" in "\n".join(
        printed(render_payload, payload, "fleet")
    )


# Help words: the runbook's "Words" list is the vocabulary; retired words stay out.


def _help_strings(parser, path=()):
    """Every help text, description, epilog, metavar, flag and destination.

    Choices are not walked: `--source job` is an API value, not a word of ours.
    """
    for action in parser._actions:
        for text in (action.help, action.metavar, action.dest, *action.option_strings):
            if isinstance(text, str):
                yield path, text
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _help_strings(child, (*path, str(name)))
    for text in (parser.description, parser.epilog):
        if text:
            yield path, text


_HELP = list(_help_strings(_parser()))
# Words split on `_` and `-` too, so a flag or destination like `job_id` is caught.
_JOB = re.compile(r"(?<![a-z])jobs?(?![a-z])", re.IGNORECASE)
_APPLICATION = re.compile(r"(?<![a-z])applications?(?![a-z])|recipe job", re.IGNORECASE)
_DRAFT = re.compile(r"(?<![a-z])draft(?![a-z])", re.IGNORECASE)


def test_help_walk_reaches_the_subcommands():
    assert {("fleet", "progress"), ("profile", "load")} <= {p for p, _ in _HELP}
    assert any(text == "--load" for _, text in _HELP)


def test_fleet_help_says_operation_not_job():
    assert [t for p, t in _HELP if p[:1] == ("fleet",) and _JOB.search(t)] == []


def test_help_uses_load_not_application():
    assert [t for _, t in _HELP if _APPLICATION.search(t)] == []


def test_help_says_draft_only_for_unsaved_edits():
    assert [t for _, t in _HELP if _DRAFT.search(t) and "unsaved" not in t] == []


def test_rendered_profile_text_uses_load_words(printed):
    texts = []
    for application_id in (None, "load-1"):
        payload = {
            "number": 1,
            "application_id": application_id,
            "application_state": "succeeded",
            "observed_at": "2026-09-23T12:59:31Z",
            "assignments": [],
        }
        texts += printed(render_payload, payload, "profile", action="endpoint")
    assert [t for t in texts if _APPLICATION.search(t)] == []
    assert any("vonkctl job run" in t for t in texts)


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

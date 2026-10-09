from __future__ import annotations

import json
from typing import cast

import pytest
from library_route_fixtures import _recipe_projection

from cluster_profiles import cli, controller_cli
from cluster_profiles.cli_select import SelectorError
from cluster_profiles.control_client import ControlClientError
from control.tests.consumer_outcomes import not_adopted


def recipe(selector: str, title: str):
    return _recipe_projection(selector, title)


class Pages:
    request_timeout_seconds = 15.0

    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload, kwargs))
        return next(self.pages)

    def profile_endpoints(self, number, alias=None):
        raise AssertionError("recipe selection must not inspect profile endpoints")


def test_recipe_selection_checks_later_pages_before_accepting_a_title():
    client = Pages(
        [
            {"recipes": [recipe("one/code", "Coding")], "next_cursor": "page-two"},
            {"recipes": [recipe("two/code", "Coding")], "next_cursor": None},
        ]
    )
    with not_adopted() as failures:
        controller_cli._resolve_recipe_selector(client, "Coding")
    assert cast(SelectorError, failures[0]).candidates == ("one/code", "two/code")
    assert client.calls[1][3]["query"]["cursor"] == "page-two"
    assert all(call[3]["query"]["assess"] is False for call in client.calls)


def test_recipe_readiness_flags_are_forwarded_to_the_controller(capsys):
    client = Pages([{"recipes": [], "next_cursor": None}])
    assert (
        cli.main(
            ["--json", "recipe", "library", "--ready", "--fits-fleet"],
            control_client=client,
        )
        == 0
    )
    query = client.calls[0][3]["query"]
    assert query["ready"] is True and query["fits_fleet"] is True
    assert json.loads(capsys.readouterr().out)["recipes"] == []


def test_recipe_engine_and_creator_filters_are_forwarded(capsys):
    client = Pages([{"recipes": [], "next_cursor": None}])
    assert (
        cli.main(
            [
                "--json",
                "recipe",
                "library",
                "--engine",
                "vllm",
                "--engine",
                "sglang",
                "--creator",
                "MiaAI-Lab",
            ],
            control_client=client,
        )
        == 0
    )
    query = client.calls[0][3]["query"]
    assert query["engine"] == ["vllm", "sglang"]
    assert query["creator"] == ["MiaAI-Lab"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--engine", "vllm,sglang", "--sparks", "1,2", "--usage", "chat, code"],
        [
            "--engine",
            "vllm",
            "--engine",
            "sglang",
            "--sparks",
            "1",
            "--sparks",
            "2",
            "--usage",
            "chat",
            "--usage",
            "code",
        ],
        [
            "--engine",
            "vllm,sglang,vllm",
            "--engine",
            " sglang, ,",
            "--sparks",
            "1,2",
            "--sparks",
            "2",
            "--usage",
            "chat,",
            "--usage",
            "code",
        ],
    ],
)
def test_multi_value_filters_take_commas_repetition_or_both(capsys, arguments):
    client = Pages([{"recipes": [], "next_cursor": None}])
    assert (
        cli.main(
            ["--json", "recipe", "library", *arguments],
            control_client=client,
        )
        == 0
    )
    query = client.calls[0][3]["query"]
    assert query["engine"] == ["vllm", "sglang"]
    assert query["sparks"] == [1, 2]
    assert query["usage"] == ["chat", "code"]


def test_multi_value_filter_refuses_a_non_numeric_spark_count(capsys):
    client = Pages([])
    assert (
        cli.main(["recipe", "library", "--sparks", "1,two"], control_client=client) != 0
    )
    assert client.calls == []
    assert "--sparks" in capsys.readouterr().err


@pytest.mark.parametrize(
    "selector", ["two/code", "11111111-1111-4111-8111-111111111111"]
)
def test_recipe_identity_wins_over_a_matching_display_title(selector):
    target = recipe("two/code", "Second")
    identity = target["identity"]
    assert isinstance(identity, dict)
    identity["recipe_id"] = "11111111-1111-4111-8111-111111111111"
    client = Pages(
        [
            {"recipes": [recipe("one/code", selector)], "next_cursor": "second"},
            {"recipes": [target], "next_cursor": None},
        ]
    )
    assert controller_cli._resolve_recipe_selector(client, selector) == selector
    assert client.calls == []


def test_cycle_restarts_complete_selection_and_fresh_selection_works():
    client = Pages(
        [
            {"recipes": [], "next_cursor": "first"},
            {"recipes": [], "next_cursor": "second"},
            {"recipes": [], "next_cursor": "first"},
            {"recipes": [recipe("one/code", "Coding")], "next_cursor": None},
            {"recipes": [recipe("two/code", "Fresh")], "next_cursor": None},
        ]
    )
    assert controller_cli._resolve_recipe_selector(client, "Coding") == "one/code"
    assert "cursor" not in client.calls[3][3]["query"]
    assert controller_cli._resolve_recipe_selector(client, "Fresh") == "two/code"


def test_selection_budget_covers_every_page_and_discards_late_results(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(controller_cli.observation.time, "monotonic", lambda: clock[0])

    class SlowPages(Pages):
        def request(self, *args, **kwargs):
            result = super().request(*args, **kwargs)
            clock[0] += 20
            return result

    client = SlowPages(
        [
            {"recipes": [], "next_cursor": "second"},
            {"recipes": [recipe("one/code", "Coding")], "next_cursor": None},
        ]
    )
    resolved = None
    try:
        resolved = controller_cli._resolve_recipe_selector(client, "Coding")
    except ControlClientError:
        pass
    assert resolved is None
    assert [call[3]["timeout_seconds"] for call in client.calls] == [30, 10]
    clock[0] = 100
    client.pages = iter(
        [{"recipes": [recipe("one/code", "Coding")], "next_cursor": None}]
    )
    assert controller_cli._resolve_recipe_selector(client, "Coding") == "one/code"


@pytest.mark.parametrize("noun,bad_row", [("recipe", None), ("spark", "invalid")])
def test_malformed_rows_restart_complete_read_before_resolving(noun, bad_row):
    if noun == "recipe":
        valid = {"recipes": [recipe("one/code", "Coding")]}
        client = Pages(
            [{"recipes": [recipe("one/code", "Coding"), bad_row]}, valid, valid]
        )
        resolver = lambda: controller_cli._resolve_recipe_selector(client, "Coding")
        expected = "one/code"
    else:
        valid = {"nodes": [{"id": "spk_" + "a" * 32, "display_name": "Atlas"}]}
        client = Pages([{"nodes": [*valid["nodes"], bad_row]}, valid, valid])
        resolver = lambda: controller_cli._resolve_spark_selectors(client, ["Atlas"])
        expected = ["spk_" + "a" * 32]
    assert resolver() == expected
    assert len(client.calls) == 2
    assert resolver() == expected


def test_spark_ambiguity_returns_usable_ids_and_exact_id_has_priority():
    first, second = "spk_" + "a" * 32, "spk_" + "b" * 32
    rows = [{"id": node, "display_name": "Atlas"} for node in (first, second)]
    with not_adopted() as failures:
        controller_cli._resolve_spark_selectors(Pages([{"nodes": rows}]), ["Atlas"])
    assert cast(SelectorError, failures[0]).candidates == (first, second)
    rows[1]["display_name"] = first
    assert controller_cli._resolve_spark_selectors(
        Pages([{"nodes": rows}]), [first]
    ) == [first]


def test_failed_selection_never_saves_a_profile(capsys):
    client = Pages(
        [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 1,
                "revision": 1,
                "definition": {"name": "Coding", "assignments": []},
            },
            {"recipes": [recipe("one/code", "Coding"), recipe("two/code", "Coding")]},
        ]
    )
    assert (
        cli.main(
            (
                "--profile",
                "1",
                "profile",
                "add",
                "Coding",
                "--spark",
                "Atlas",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    assert "operation_id" not in json.loads(capsys.readouterr().out)
    assert all(call[0] == "GET" for call in client.calls)


def test_ambiguity_keeps_every_candidate_outside_bounded_error_copy(capsys):
    rows = [recipe(f"publisher-{index:03}/code", "Coding") for index in range(100)]
    client = Pages(
        [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 1,
                "revision": 1,
                "definition": {"name": "Coding", "assignments": []},
            },
            {"recipes": rows, "next_cursor": None},
        ]
    )
    assert (
        cli.main(
            (
                "--profile",
                "1",
                "profile",
                "add",
                "Coding",
                "--spark",
                "Atlas",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["candidates"] == [row["selector"] for row in rows]


def test_a_selector_prefix_does_not_select_a_different_variant():
    client = Pages(
        [
            {"recipes": [recipe("one/code-nvfp4", "Coding NVFP4")]},
            {"selector": "one/code"},
        ]
    )
    assert controller_cli._resolve_recipe_selector(client, "one/code") == "one/code"
    assert client.calls == []


def test_recipe_alternatives_render_one_comparable_line_each(capsys):
    from cluster_profiles import cli_render

    cli_render._recipe_alternatives(
        {
            "alternatives": [
                {
                    "selector": "vonk-forge/glm-sglang-dual",
                    "engine": "sglang",
                    "node_count": 2,
                    "creator": "MiaAI-Lab",
                    "version": "1.7.2",
                    "cache": "not_cached",
                    "fits_fleet": "ready",
                }
            ]
        }
    )
    out = capsys.readouterr().out
    assert (
        "vonk-forge/glm-sglang-dual  sglang, 2 Sparks, MiaAI-Lab, v1.7.2, "
        "not cached, fits fleet"
    ) in out
    cli_render._recipe_alternatives({"alternatives": []})
    assert capsys.readouterr().out == ""

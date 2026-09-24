from __future__ import annotations

import json

import pytest

from cluster_profiles import cli, controller_cli
from cluster_profiles.cli_select import SelectorError


def recipe(selector: str, title: str) -> dict[str, object]:
    publisher, slug = selector.split("/")
    return {
        "selector": selector,
        "identity": {"publisher": publisher, "slug": slug, "title": title},
    }


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
    with pytest.raises(SelectorError) as error:
        controller_cli._resolve_recipe_selector(client, "Coding")
    assert error.value.candidates == ("one/code", "two/code")
    assert client.calls[1][3]["query"]["cursor"] == "page-two"
    assert all(call[3]["query"]["assess"] is False for call in client.calls)


def test_recipe_readiness_flags_are_forwarded_to_the_controller(capsys):
    client = Pages([{"recipes": [], "next_cursor": None}])
    assert (
        cli.main(
            ["--json", "recipe", "library", "--all-models", "--ready", "--fits-fleet"],
            control_client=client,
        )
        == 0
    )
    query = client.calls[0][3]["query"]
    assert query["ready"] is True and query["fits_fleet"] is True
    assert json.loads(capsys.readouterr().out)["recipes"] == []


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
    assert controller_cli._resolve_recipe_selector(client, selector) == "two/code"
    assert (
        len(client.calls) == 2
    )  # An unverified publisher/slug must not bypass lookup.


def test_cycle_aborts_selection_before_another_request():
    client = Pages(
        [
            {"recipes": [], "next_cursor": "first"},
            {"recipes": [], "next_cursor": "second"},
            {"recipes": [], "next_cursor": "first"},
        ]
    )
    with pytest.raises(SelectorError, match="cursor.*repeated"):
        controller_cli._resolve_recipe_selector(client, "Missing")
    assert len(client.calls) == 3


def test_selection_budget_covers_every_page_and_discards_late_results(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(controller_cli.time, "monotonic", lambda: clock[0])

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
    with pytest.raises(SelectorError, match="deadline"):
        controller_cli._resolve_recipe_selector(client, "Coding")
    assert [call[3]["timeout_seconds"] for call in client.calls] == [30, 10]


@pytest.mark.parametrize("noun,bad_row", [("recipe", None), ("spark", "invalid")])
def test_malformed_rows_cannot_be_discarded_to_create_a_unique_match(noun, bad_row):
    if noun == "recipe":
        client = Pages([{"recipes": [recipe("one/code", "Coding"), bad_row]}])
        resolver = lambda: controller_cli._resolve_recipe_selector(client, "Coding")
    else:
        client = Pages(
            [{"nodes": [{"id": "spk_" + "a" * 32, "display_name": "Atlas"}, bad_row]}]
        )
        resolver = lambda: controller_cli._resolve_spark_selectors(client, ["Atlas"])
    with pytest.raises((TypeError, ValueError), match="invalid"):
        resolver()


def test_spark_ambiguity_returns_usable_ids_and_exact_id_has_priority():
    first, second = "spk_" + "a" * 32, "spk_" + "b" * 32
    rows = [{"id": node, "display_name": "Atlas"} for node in (first, second)]
    with pytest.raises(SelectorError) as error:
        controller_cli._resolve_spark_selectors(Pages([{"nodes": rows}]), ["Atlas"])
    assert error.value.candidates == (first, second)
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
    assert "ambiguous" in json.loads(capsys.readouterr().out)["error"]
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
    client = Pages([{"recipes": [recipe("one/code-nvfp4", "Coding NVFP4")]}])
    with pytest.raises(SelectorError, match="unknown"):
        controller_cli._resolve_recipe_selector(client, "one/code")


def test_node_profile_is_a_focused_read_of_the_canonical_node(capsys):
    node = {
        "id": "spk_" + "a" * 32,
        "display_name": "Atlas",
        "hostname": "atlas.local",
        "lifecycle": "managed",
        "labels": {"room": "lab"},
    }
    client = Pages([{"nodes": [node]}, node])
    assert cli.main(("fleet", "node-profile", "Atlas"), control_client=client) == 0
    output = capsys.readouterr().out
    assert "room=lab" in output and node["id"] in output and "managed" in output
    assert "Running workloads" not in output
    assert [(call[0], call[1]) for call in client.calls] == [
        ("GET", "/api/fleet"),
        ("GET", "/api/fleet/" + node["id"]),
    ]

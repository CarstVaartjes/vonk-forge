from __future__ import annotations

import io
import json

from cluster_profiles import cli

NODE = "spk_" + "1" * 32
SELECTOR = "vonk-forge/glm"
OPTIONS = [
    {
        "name": "verification",
        "label": "Verification",
        "help": "How drafted tokens are verified.",
        "choices": [
            {"value": "standard", "label": "Standard", "help": "h", "default": True},
            {"value": "adaptive-k", "label": "Adaptive", "help": "h"},
        ],
    },
    {
        "name": "projections",
        "label": "Projections",
        "help": "Dense projection precision.",
        "choices": [
            {"value": "stock", "label": "Stock", "help": "h", "default": True},
            {"value": "dense-fp8", "label": "FP8", "help": "h"},
        ],
    },
]


class Fake:
    request_timeout_seconds = 15.0

    def __init__(self, assignments=None):
        self.definition = {"name": "P", "assignments": assignments or []}
        self.saved = None

    def request(self, method, path, payload=None, **kwargs):
        if path == "/api/fleet":
            return {"nodes": [{"id": NODE, "display_name": "Atlas"}]}
        if path == "/api/recipe/library":
            return {
                "recipes": [
                    {
                        "selector": SELECTOR,
                        "identity": {
                            "publisher": "vonk-forge",
                            "slug": "glm",
                            "title": "GLM",
                        },
                    }
                ],
                "next_cursor": None,
            }
        if path.startswith("/api/recipe/vonk-forge"):
            return {"selector": SELECTOR, "document": {"options": OPTIONS}}
        if method == "GET" and path.endswith("/definition"):
            return {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 1,
                "revision": 1,
                "definition": self.definition,
            }
        if method == "PUT":
            self.saved = payload
            return {"revision": 2, "definition": dict(payload or {})}
        raise AssertionError((method, path))


def _saved(client: Fake) -> dict:
    assert client.saved is not None
    return client.saved


def _add(client, *extra, capsys=None):
    return cli.main(
        (
            "--profile",
            "1",
            "profile",
            "add",
            SELECTOR,
            "--spark",
            "Atlas",
            "--json",
            *extra,
        ),
        control_client=client,
    )


def test_option_flag_is_saved_and_others_default(capsys):
    client = Fake()
    assert _add(client, "--option", "verification=adaptive-k", "--no-input") == 0
    [assignment] = _saved(client)["assignments"]
    assert assignment["option_choices"] == {
        "verification": "adaptive-k",
        "projections": "stock",
    }


def test_unknown_value_is_refused_with_the_choices(capsys):
    client = Fake()
    assert _add(client, "--option", "verification=nope") != 0
    assert client.saved is None
    assert "adaptive-k" in capsys.readouterr().out


def test_no_input_without_options_is_not_an_error_and_sends_no_choice(capsys):
    client = Fake()
    assert _add(client, "--no-input") == 0
    # The Controller fills every default when the profile is saved.
    assert not _saved(client)["assignments"][0].get("option_choices")


def test_interactive_prompt_accepts_default_with_enter(monkeypatch, capsys):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty("\ndense-fp8\n"))
    monkeypatch.setattr("sys.stderr", Tty())
    client = Fake()
    # The fake's save receipt is not a full profile view, so only the save
    # itself is asserted here.
    cli.main(
        ("--profile", "1", "profile", "add", SELECTOR, "--spark", "Atlas"),
        control_client=client,
    )
    assert _saved(client)["assignments"][0]["option_choices"] == {
        "verification": "standard",
        "projections": "dense-fp8",
    }


def test_configure_changes_choices_and_keeps_the_rest(capsys):
    client = Fake(
        [
            {
                "recipe_selector": SELECTOR,
                "spark_ids": [NODE],
                "option_choices": {
                    "verification": "adaptive-k",
                    "projections": "stock",
                },
            }
        ]
    )
    assert (
        cli.main(
            (
                "--profile",
                "1",
                "profile",
                "configure",
                "--option",
                "projections=dense-fp8",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert _saved(client)["assignments"][0]["option_choices"] == {
        "verification": "adaptive-k",
        "projections": "dense-fp8",
    }


def test_profile_show_lists_chosen_values(capsys):
    from cluster_profiles import cli_render

    cli_render.render_payload(
        {
            "number": 1,
            "name": "P",
            "definition": {
                "assignments": [
                    {
                        "recipe_selector": SELECTOR,
                        "spark_ids": [NODE],
                        "option_choices": {"verification": "adaptive-k"},
                    }
                ]
            },
            "assignments": [],
        },
        "profile",
    )
    assert "verification" in capsys.readouterr().out


def test_recipe_detail_lists_options_and_marks_the_default(capsys):
    from cluster_profiles import cli_render

    item = {
        "selector": SELECTOR,
        "identity": {"publisher": "vonk-forge", "slug": "glm", "title": "GLM"},
        "local": {},
        "resources": {},
        "document": {"options": OPTIONS},
    }
    cli_render._library_item(item, "recipe", detail=True)
    out = capsys.readouterr().out
    assert "adaptive-k" in out and "(default)" in out


def test_definition_import_round_trips_option_choices(tmp_path, capsys):
    client = Fake()
    source = tmp_path / "d.json"
    definition = {
        "name": "P",
        "assignments": [
            {
                "recipe_selector": SELECTOR,
                "spark_ids": [NODE],
                "option_choices": {"verification": "adaptive-k"},
            }
        ],
    }
    source.write_text(json.dumps(definition))
    assert (
        cli.main(
            (
                "--profile",
                "1",
                "profile",
                "import",
                "--file",
                str(source),
                "--expected-revision",
                "1",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert _saved(client)["assignments"][0]["option_choices"] == {
        "verification": "adaptive-k"
    }

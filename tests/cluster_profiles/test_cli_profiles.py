from __future__ import annotations

import copy
import io
import json
import os
import sys
from pathlib import Path

import pytest

from cluster_profiles import cli
from cluster_profiles.cli_files import read_json_document, write_private_document
from cluster_profiles.control_client import MAX_CONTROL_DOCUMENT_BYTES


class ProfileClient:
    def __init__(self):
        self.definition = {
            "name": "Coding",
            "description": "Preserve me",
            "favorite": True,
            "installation_policy": "exact",
            "labels": {"use": "code"},
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/qwen-code",
                    "spark_ids": ["spk_" + "a" * 32],
                    "assignment_name": "coding",
                    "model_variant": "nvfp4",
                    "desired_state": "installed",
                }
            ],
        }
        self.calls = []
        self.saved = None

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        if method == "PUT":
            assert isinstance(payload, dict)
            self.saved = copy.deepcopy(payload)
            return {
                "number": 2,
                "revision": 8,
                "definition": {
                    key: value
                    for key, value in payload.items()
                    if key != "expected_revision"
                },
            }
        if path == "/api/profile/2/definition":
            return {
                "schema_version": 2,
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 2,
                "revision": 7,
                "definition": copy.deepcopy(self.definition),
            }
        if path == "/api/fleet":
            return {
                "nodes": [
                    {"id": "spk_" + "a" * 32, "display_name": "Atlas"},
                    {"id": "spk_" + "b" * 32, "display_name": "Boreal"},
                ]
            }
        if path == "/api/recipe/library":
            return {
                "recipes": [
                    {
                        "selector": "vonk-forge/qwen-code",
                        "identity": {
                            "publisher": "vonk-forge",
                            "slug": "qwen-code",
                            "title": "Qwen Code",
                        },
                    }
                ],
                "next_cursor": None,
            }
        raise AssertionError(path)


def test_name_change_preserves_complete_authoring_definition(capsys):
    client = ProfileClient()
    assert (
        cli.main(
            ("--profile", "2", "profile", "name", "New name", "--json"),
            control_client=client,
        )
        == 0
    )
    assert client.saved == {
        **client.definition,
        "name": "New name",
        "expected_revision": 7,
    }
    assert client.calls == [
        ("GET", "/api/profile/2/definition"),
        ("PUT", "/api/profile/2"),
    ]


def test_configure_changes_only_explicit_fields(capsys):
    client = ProfileClient()
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "configure",
                "--favorite",
                "false",
                "--description",
                "",
                "--label",
                "team=research",
                "--remove-label",
                "use",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert client.saved == {
        **client.definition,
        "favorite": False,
        "description": "",
        "labels": {"team": "research"},
        "expected_revision": 7,
    }


def test_export_and_import_roundtrip_without_loading(tmp_path: Path, capsys):
    client = ProfileClient()
    destination = tmp_path / "profile.json"
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "export",
                "--output",
                str(destination),
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(destination.read_text()) == client.definition
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "import",
                "--file",
                str(destination),
                "--expected-revision",
                "7",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert client.saved == {**client.definition, "expected_revision": 7}
    assert all(method != "POST" for method, _ in client.calls)


def test_add_spark_preserves_existing_installed_state_and_variant(capsys):
    client = ProfileClient()
    before = copy.deepcopy(client.definition)
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "add",
                "vonk-forge/qwen-code",
                "--as",
                "coding",
                "--spark",
                "Boreal",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    before["assignments"][0]["spark_ids"].append("spk_" + "b" * 32)
    assert client.saved == {**before, "expected_revision": 7}


@pytest.mark.parametrize(
    "edit",
    [
        ("--label", "team=one", "--label", "team=two"),
        ("--label", "use=one", "--remove-label", "use"),
        ("--remove-label", "missing"),
    ],
)
def test_invalid_label_intent_does_not_write(edit, capsys):
    client = ProfileClient()
    assert (
        cli.main(
            ("--profile", "2", "profile", "configure", *edit, "--json"),
            control_client=client,
        )
        == 2
    )
    assert client.saved is None


def test_malformed_saved_assignment_is_not_discarded_or_replaced(capsys):
    client = ProfileClient()
    client.definition["assignments"][0]["spark_ids"] = "not-an-array"
    assert (
        cli.main(
            ("--profile", "2", "profile", "name", "Renamed", "--json"),
            control_client=client,
        )
        == 2
    )
    assert client.saved is None


def test_remove_refuses_a_spark_that_is_not_in_the_assignment(capsys):
    client = ProfileClient()
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "remove",
                "coding",
                "--spark",
                "Atlas",
                "--spark",
                "Boreal",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    assert client.saved is None


def test_edit_revision_must_match_the_definition_that_was_read(capsys):
    client = ProfileClient()
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "name",
                "New",
                "--expected-revision",
                "8",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    assert client.saved is None


@pytest.mark.parametrize(
    "content",
    [
        '{"favorite": false, "favorite": true}',
        '{"favorite": NaN}',
        '{"favorite": "false"}',
        '{"assignments": [{"recipe_selector": "vonk-forge/qwen-code"}]}',
        '{"name": "New", "revision": 7}',
    ],
)
def test_invalid_import_fails_before_any_request(tmp_path, content, capsys):
    source = tmp_path / "input.json"
    source.write_text(content)
    client = ProfileClient()
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "import",
                "--file",
                str(source),
                "--expected-revision",
                "7",
                "--json",
            ),
            control_client=client,
        )
        == 2
    )
    assert client.calls == []


def test_stdout_export_remains_a_json_definition_without_json_flag(capsys):
    client = ProfileClient()
    assert cli.main(("--profile", "2", "profile", "export"), control_client=client) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == client.definition
    assert captured.err == ""


def test_import_accepts_piped_definition_with_explicit_revision(monkeypatch, capsys):
    client = ProfileClient()
    stream = io.TextIOWrapper(io.BytesIO(json.dumps(client.definition).encode()))
    monkeypatch.setattr(sys, "stdin", stream)
    assert (
        cli.main(
            (
                "--profile",
                "2",
                "profile",
                "import",
                "--file",
                "-",
                "--expected-revision",
                "7",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert client.saved == {**client.definition, "expected_revision": 7}
    assert client.calls == [("PUT", "/api/profile/2")]


def test_exports_never_overwrite_an_existing_file_or_symlink(tmp_path):
    existing = tmp_path / "existing.json"
    existing.write_text("private existing content")
    link = tmp_path / "linked.json"
    link.symlink_to(existing)
    for target in (existing, link):
        with pytest.raises(FileExistsError):
            write_private_document(target, {"name": "New"})
    assert existing.read_text() == "private existing content"
    with pytest.raises(OSError):
        read_json_document(str(link))


def test_nonregular_and_oversized_imports_are_refused(tmp_path):
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        read_json_document(str(fifo))
    large = tmp_path / "large.json"
    large.write_bytes(b" " * (MAX_CONTROL_DOCUMENT_BYTES + 1))
    with pytest.raises(ValueError, match=str(MAX_CONTROL_DOCUMENT_BYTES)):
        read_json_document(str(large))


def test_failed_export_removes_only_its_partial_file(tmp_path, monkeypatch):
    def failed_flush(descriptor):
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", failed_flush)
    destination = tmp_path / "export.json"
    with pytest.raises(OSError, match="disk unavailable"):
        write_private_document(destination, {"name": "New"})
    assert not destination.exists()

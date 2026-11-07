"""Every vonkctl-only and job document validates against its published schema.

The Controller-backed fleet, profile and recipe documents are proven against the
real API in control/tests/test_vonkctl_json_contract.py; this file proves the
documents only the CLI prints, and the job documents, which need no API.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

# The artifact-job fixtures own the Controller job shapes and their clock.
from test_cli_artifact_jobs import (  # noqa: F401
    JOB_ID,
    RUN_ID,
    ArtifactJobClient,
    _binding,
    _succeeded_job,
    artifact_clock,
)

from cluster_profiles import cli
from cluster_profiles.cli_json_contract import (
    DOCUMENT_VERSIONS,
    DocumentKind,
    document_kind,
    stamp,
)

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "vonkctl"


def _validate(kind: DocumentKind, document: dict) -> None:
    schema = json.loads(
        (SCHEMAS / f"{kind}-v{DOCUMENT_VERSIONS[kind]}.schema.json").read_text()
    )
    assert document["schema_version"] == DOCUMENT_VERSIONS[kind]
    jsonschema.Draft202012Validator(schema).validate(document)


def _run(client, capsys, *argv: str, request_key: str | None = None) -> dict:
    status = cli.main(
        ("--json", *argv),
        control_client=client,
        request_id_factory=(lambda: request_key) if request_key else None,
    )
    document = json.loads(capsys.readouterr().out)
    assert status == 0, document
    return document


def test_job_and_download_documents_match_their_schemas(tmp_path, capsys) -> None:
    client = ArtifactJobClient()
    client.job = _succeeded_job()
    output = tmp_path / "outputs"
    output.mkdir()

    _validate(
        DocumentKind.ARTIFACT_JOB,
        _run(client, capsys, "recipe", "job", "detail", JOB_ID),
    )
    download = _run(
        client, capsys, "recipe", "job", "download", JOB_ID, "--output", str(output)
    )
    _validate(DocumentKind.ARTIFACT_DOWNLOAD, download)
    assert download["files"][0]["state"] == "downloaded"
    reused = _run(
        client, capsys, "recipe", "job", "download", JOB_ID, "--output", str(output)
    )
    _validate(DocumentKind.ARTIFACT_DOWNLOAD, reused)
    assert reused["files"][0]["state"] == "reused"


def test_job_create_document_matches_its_schema(tmp_path, capsys) -> None:
    client = ArtifactJobClient()
    binding = _binding(tmp_path / "job.json", {"input.png": b"x"})
    created = _run(
        client,
        capsys,
        "recipe",
        "job",
        "create",
        "--run",
        RUN_ID,
        "--file",
        str(binding),
        request_key="00000000-0000-4000-8000-000000000003",
    )
    _validate(DocumentKind.ARTIFACT_JOB, created)


class _Reachable:
    request_timeout_seconds = 15.0

    def request(self, method, path, payload=None, **kwargs):
        return {}


def test_connection_document_matches_its_schema(capsys, monkeypatch) -> None:
    monkeypatch.setenv("VONK_CONTROL_URL", "https://forge.example.test")
    _validate(DocumentKind.CONNECTION, _run(_Reachable(), capsys, "--check-connection"))
    monkeypatch.delenv("VONK_CONTROL_URL")
    document = _run(_Reachable(), capsys, "--check-connection")
    _validate(DocumentKind.CONNECTION, document)
    assert document["origin"] is None


def test_usage_error_document_matches_its_schema(capsys) -> None:
    assert cli.main(("--json", "profile", "no-such-action")) == 2
    _validate(DocumentKind.ERROR, json.loads(capsys.readouterr().out))


def test_every_kind_is_reachable_from_a_command() -> None:
    """A kind in the table that no command can print would be a dead contract."""

    parser = cli._parser()
    printed = {
        document_kind(parser.parse_args(argv), {}, error=error)
        for argv, error in (
            (("--check-connection",), False),
            (("fleet",), False),
            (("profile", "list"), False),
            (("profile",), False),
            (("profile", "load", "--review"), False),
            (("profile", "progress"), False),
            (("profile", "endpoint"), False),
            (("recipe",), False),
            (("recipe", "detail", "x"), False),
            (("recipe", "job", "detail", JOB_ID), False),
            (("recipe", "job", "download", JOB_ID, "--output", "x"), False),
            (("fleet",), True),
        )
    } | {
        document_kind(
            parser.parse_args(("fleet",)),
            {"result": {}, "observation": {}},
            error=False,
        )
    }
    assert printed == set(DOCUMENT_VERSIONS)


def test_a_document_without_a_kind_is_not_stamped() -> None:
    assert stamp({"nodes": []}, None) == {"nodes": []}
    assert stamp(
        {"schema_version": 2, "allowed": True}, DocumentKind.PROFILE_REVIEW
    ) == {
        "schema_version": 2,
        "allowed": True,
    }

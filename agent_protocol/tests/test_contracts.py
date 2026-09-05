from __future__ import annotations

import hashlib
import importlib.resources
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation,
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
    schema_validator,
    validate_schema_message,
)


def valid_claim() -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "attempt": 1,
        "fence": "00000000-0000-4000-8000-000000000003",
        "node_id": "spk_00000000000000000000000000000001",
        "operation": "node.probe",
        "authority_revision": "a" * 64,
        "payload_digest": hashlib.sha256(b"{}").hexdigest(),
        "payload": {},
        "deadline": "2026-08-03T12:00:00+00:00",
    }


def valid_attempt() -> dict[str, object]:
    return {
        key: valid_claim()[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    }


def claim_with_payload(payload: dict[str, str]) -> dict[str, object]:
    return valid_claim() | {
        "payload": payload,
        "payload_digest": hashlib.sha256(canonical_message(payload)).hexdigest(),
    }


def claim_for_operation(
    operation: str, payload: dict[str, object]
) -> dict[str, object]:
    return valid_claim() | {
        "operation": operation,
        "payload": payload,
        "payload_digest": hashlib.sha256(canonical_message(payload)).hexdigest(),
    }


def recipe_build_vectors() -> dict[str, object]:
    return json.loads(
        (
            importlib.resources.files("vonk_agent_protocol")
            / "vectors"
            / "recipe-build-claim-v1.json"
        ).read_text(encoding="utf-8")
    )


def apply_vector_changes(
    base: dict[str, object], changes: list[dict[str, object]]
) -> dict[str, object]:
    payload = deepcopy(base)
    for change in changes:
        path = change["path"]
        assert isinstance(path, list) and path
        target: object = payload
        for component in path[:-1]:
            assert isinstance(target, (dict, list))
            target = target[component]  # type: ignore[index]
        assert isinstance(target, (dict, list))
        if change["op"] == "set":
            target[path[-1]] = deepcopy(change["value"])  # type: ignore[index]
        else:
            assert change["op"] == "remove"
            del target[path[-1]]  # type: ignore[index]
    return payload


PATH_KEY_TOKENS = ("path", "file", "filename", "filepath", "directory", "folder")
FORBIDDEN_PATH_KEY_FORMS = (
    "{token}",
    "{token}_value",
    "{token}-value",
    "{token}Value",
    "{upper}",
    "{upper}_value",
    "{upper}-value",
    "{upper}Value",
    "artifact_{token}",
    "artifact_{token}_value",
    "artifact-{token}",
    "artifact-{token}-value",
    "artifact{title}",
    "artifact{title}_value",
    "artifact{title}-value",
    "artifact{title}Value",
    "artifact{upper}",
    "artifact{upper}_value",
    "artifact{upper}-value",
    "artifact{upper}Value",
)
SAFE_PATH_KEY_COLLISIONS = (
    "profile",
    "pathology",
    "Pathology",
    "filetype",
    "FILEtype",
    "filenameish",
    "filepathish",
    "directoryish",
    "folderish",
    "artifactPathology",
    "artifactFiletype",
    "artifactFilenameish",
    "artifactFilepathish",
    "someDirectoryish",
    "someFolderish",
    "artifactPATHology",
    "artifactFILEtype",
    "someDIRECTORYish",
    "someFOLDERish",
    "filesystem",
    "mount",
)


def test_claim_is_node_scoped_and_canonical() -> None:
    claim = AgentClaim.parse(valid_claim())

    assert json.loads(canonical_message(claim))["operation"] == "node.probe"


@pytest.mark.parametrize("field", ["command", "shell", "environment", "password"])
def test_protocol_rejects_execution_and_secret_fields(field: str) -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {field: "unsafe"}})


def test_protocol_rejects_unsafe_keys_recursively() -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {"safe": {"apiToken": "unsafe"}}})


def test_protocol_allows_only_exact_versioned_platform_target_identifier() -> None:
    target = "platform/releases/1.2.3/" + "a" * 64 + ".json"

    claim = AgentClaim.parse(claim_with_payload({"platform_target_name": target}))

    assert claim.payload["platform_target_name"] == target


@pytest.mark.parametrize(
    "target",
    (
        "platform-release.json",
        "platform/releases/latest/" + "a" * 64 + ".json",
        "platform/releases/1.2.3/../../escape.json",
        "platform/releases/1.2.3/" + "A" * 64 + ".json",
    ),
)
def test_protocol_rejects_noncanonical_platform_target_identifier(
    target: str,
) -> None:
    with pytest.raises(AgentProtocolError, match="platform target"):
        AgentClaim.parse(claim_with_payload({"platform_target_name": target}))


def test_claim_rejects_changed_payload_digest() -> None:
    with pytest.raises(AgentProtocolError, match="digest"):
        AgentClaim.parse(valid_claim() | {"payload": {"healthy": True}})


@pytest.mark.parametrize(
    "deadline",
    ["2026-08-03T12:00:00", "2026-08-03T12:00:00+02:00"],
)
def test_claim_requires_an_aware_utc_deadline(deadline: str) -> None:
    with pytest.raises(AgentProtocolError, match="deadline"):
        AgentClaim.parse(valid_claim() | {"deadline": deadline})


def test_claim_copies_canonical_payload_before_becoming_frozen() -> None:
    source = valid_claim() | {"payload": {"nested": ["before"]}}
    source["payload_digest"] = hashlib.sha256(
        canonical_message(source["payload"])
    ).hexdigest()
    claim = AgentClaim.parse(source)
    source["payload"]["nested"].append("after")  # type: ignore[index]

    assert json.loads(canonical_message(claim))["payload"] == {"nested": ["before"]}
    with pytest.raises(AttributeError):
        claim.attempt = 2  # type: ignore[misc]


def test_direct_construction_cannot_bypass_claim_validation_or_serialization() -> None:
    raw = valid_claim()

    with pytest.raises(AgentProtocolError, match="unsafe"):
        AgentClaim(
            schema_version=1,
            job_id=raw["job_id"],
            operation_id=raw["operation_id"],
            attempt=1,
            fence=raw["fence"],
            node_id=raw["node_id"],
            operation=AgentOperation.NODE_PROBE,
            authority_revision="a" * 64,
            payload_digest=hashlib.sha256(b'{"command":"unsafe"}').hexdigest(),
            payload={"command": "unsafe"},
            deadline=datetime(2026, 8, 3, 12, tzinfo=UTC),
        )


def test_direct_result_construction_rejects_client_filesystem_paths() -> None:
    raw = valid_attempt()

    with pytest.raises(AgentProtocolError, match="path"):
        AgentResult(
            **raw,
            state="succeeded",
            result={"evidence": "/var/lib/vonk-agent/result.json"},
        )


def test_recipe_start_result_accepts_only_typed_endpoint_and_model_identity_uris() -> (
    None
):
    raw = valid_attempt()
    revision = "sha256:" + "a" * 64
    result = {
        "endpoint": "http://[fd00::211]:8000",
        "evidence": {
            "endpoint": "http://[fd00::211]:8000",
            "model_identity": (
                "https://models.example.invalid/organization/model.bin?download=true@"
                + revision
            ),
        },
    }

    parsed = AgentResult.parse(raw | {"state": "succeeded", "result": result})

    assert parsed.result["endpoint"] == "http://[fd00::211]:8000"


@pytest.mark.parametrize(
    "model_identity",
    [
        "Qwen/Qwen3-8B@" + "a" * 40,
        "ghcr.io/vonkforge/model@sha256:" + "a" * 64,
        "https://example.invalid/model.bin?download=true@sha256:" + "a" * 64,
    ],
)
def test_recipe_result_accepts_each_authorized_model_identity_form(
    model_identity: str,
) -> None:
    result = {"evidence": {"model_identity": model_identity}}

    AgentResult.parse(valid_attempt() | {"state": "succeeded", "result": result})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "/var/lib/vonk-forge/result.json"),
        ("endpoint", "http://192.168.1.211:8000/private"),
        ("endpoint", "http://worker.example.invalid:8000"),
        ("endpoint", "http://192.168.1.211:0"),
        ("model_identity", "/models/private@sha256:" + "a" * 64),
        ("model_identity", "../private@sha256:" + "a" * 64),
        (
            "model_identity",
            "https://user:password@example.invalid/model@sha256:" + "a" * 64,
        ),
        (
            "model_identity",
            "https://example.invalid/model?access_token=unsafe@sha256:" + "a" * 64,
        ),
    ],
)
def test_typed_recipe_result_uri_fields_reject_path_or_credential_confusion(
    field: str, value: str
) -> None:
    result = {field: value}
    if field == "model_identity":
        result = {"evidence": result}

    with pytest.raises(AgentProtocolError, match="path"):
        AgentResult.parse(valid_attempt() | {"state": "succeeded", "result": result})


def test_typed_result_uri_exceptions_do_not_apply_to_claims_or_progress() -> None:
    endpoint = "http://192.168.1.211:8000"

    with pytest.raises(AgentProtocolError, match="path"):
        AgentClaim.parse(claim_with_payload({"endpoint": endpoint}))
    with pytest.raises(AgentProtocolError, match="path"):
        AgentProgress(**valid_attempt(), progress={"endpoint": endpoint})


def test_direct_progress_construction_enforces_protocol_boundary() -> None:
    raw = valid_attempt()

    with pytest.raises(AgentProtocolError, match="unsafe"):
        AgentProgress(**raw, progress={"authorization": "unsafe"})


@pytest.mark.parametrize(
    "payload",
    [
        {"artifact_path": "release"},
        {"artifactPath": "release"},
        {"artifactPATH": "release"},
        {"artifactFILE": "release"},
        {"someDIRECTORY": "release"},
        {"evidence": "../private"},
    ],
)
def test_protocol_rejects_client_selected_filesystem_paths(
    payload: dict[str, str],
) -> None:
    with pytest.raises(AgentProtocolError, match="path"):
        AgentClaim.parse(valid_claim() | {"payload": payload})


def test_recipe_build_claim_accepts_only_typed_slash_bearing_fields() -> None:
    vectors = recipe_build_vectors()
    payload = deepcopy(vectors["base_payload"])
    assert isinstance(payload, dict)
    payload["arguments"] = [{"name": "runtime-source", "value": "vendor/runtime"}]

    claim = AgentClaim.parse(claim_for_operation("recipe.build.v1", payload))

    assert claim.payload["platform"] == "linux/arm64"
    assert claim.payload["base_images"][0]["reference"].startswith("ghcr.io/")


def test_recipe_build_claim_matches_shared_cross_language_vectors() -> None:
    vectors = recipe_build_vectors()
    base = vectors["base_payload"]
    cases = vectors["cases"]
    assert vectors["schema_version"] == 1
    assert isinstance(base, dict)
    assert isinstance(cases, list)

    for case in cases:
        assert isinstance(case, dict)
        payload = apply_vector_changes(base, case["changes"])
        raw = claim_for_operation("recipe.build.v1", payload)
        if case["valid"]:
            AgentClaim.parse(raw)
        else:
            with pytest.raises(AgentProtocolError, match=".+"):
                AgentClaim.parse(raw)


@pytest.mark.parametrize("capability", ["SYS_ADMIN", "SYS_CHROOT", "SYS_PTRACE"])
def test_recipe_build_claim_rejects_every_sys_capability(capability: str) -> None:
    vectors = recipe_build_vectors()
    payload = deepcopy(vectors["base_payload"])
    assert isinstance(payload, dict)
    payload["capabilities"] = [capability]

    with pytest.raises(AgentProtocolError, match="capabilities are not allowed"):
        AgentClaim.parse(claim_for_operation("recipe.build.v1", payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"platform": "linux/amd64"},
        {"dockerfile": "/etc/passwd"},
        {"dockerfile": "../Dockerfile"},
        {"dockerfile": "containers//Dockerfile"},
        {"base_images": [{"reference": "ghcr.io/vonkforge/runtime:latest"}]},
        {"evidence": "host/path"},
        {"arguments": [{"name": "safe", "nested": {"value": "host/path"}}]},
    ],
)
def test_recipe_build_claim_rejects_untyped_filesystem_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(AgentProtocolError, match="path"):
        AgentClaim.parse(claim_for_operation("recipe.build.v1", payload))


def test_signed_agent_upgrade_payload_is_accepted_by_runtime_and_schema() -> None:
    payload = {
        "architecture": "linux-arm64",
        "package_bytes": 5_000_000,
        "package_sha256": "b" * 64,
        "package_signature": "c" * 128,
        "package_url": (
            "https://install.vonkforge.ai/artifacts/dev/releases/example/"
            "spark/current/linux-arm64/vonk-forge-agent.deb"
        ),
        "package_version": "0.1.0~dev.330+g0123456789ab",
        "schema_version": 1,
        "target_binary_digest": "d" * 64,
        "target_build_digest": "sha256:" + "e" * 64,
    }
    raw = claim_for_operation("agent.upgrade.v1", payload)

    assert AgentClaim.parse(raw)
    assert schema("agent-job.schema.json").is_valid(raw)
    assert validate_schema_message("agent-job.schema.json", raw)


def protocol_message_with_document(
    name: str,
    document: dict[str, str],
) -> tuple[dict[str, object], Callable[[object], AgentClaim | AgentResult]]:
    if name == "agent-job.schema.json":
        return claim_with_payload(document), AgentClaim.parse
    return (
        valid_attempt() | {"state": "succeeded", "result": document},
        AgentResult.parse,
    )


def test_path_key_agreement_matrix_covers_exact_required_tokens() -> None:
    assert len(PATH_KEY_TOKENS) == 6
    assert set(PATH_KEY_TOKENS) == {
        "path",
        "file",
        "filename",
        "filepath",
        "directory",
        "folder",
    }


@pytest.mark.parametrize("name", ["agent-job.schema.json", "agent-result.schema.json"])
@pytest.mark.parametrize("token", PATH_KEY_TOKENS)
@pytest.mark.parametrize("form", FORBIDDEN_PATH_KEY_FORMS)
def test_complete_path_key_segments_are_rejected_by_runtime_and_schemas(
    name: str,
    token: str,
    form: str,
) -> None:
    # A token starts at the key edge, after '_'/'-', or uppercase after
    # lowercase/digit. It ends at the key edge, before '_'/'-', or before an
    # uppercase continuation. A lowercase continuation remains safe.
    field = form.format(token=token, title=token.title(), upper=token.upper())
    raw, parser = protocol_message_with_document(name, {field: "release"})

    with pytest.raises(AgentProtocolError, match="path"):
        parser(raw)
    assert not schema(name).is_valid(raw)
    with pytest.raises(AgentProtocolError):
        validate_schema_message(name, raw)


@pytest.mark.parametrize("name", ["agent-job.schema.json", "agent-result.schema.json"])
@pytest.mark.parametrize("field", SAFE_PATH_KEY_COLLISIONS)
def test_path_token_collisions_are_accepted_by_runtime_and_schemas(
    name: str,
    field: str,
) -> None:
    raw, parser = protocol_message_with_document(name, {field: "release"})

    assert parser(raw)
    assert schema(name).is_valid(raw)
    assert validate_schema_message(name, raw)


def test_progress_and_result_are_fenced_node_messages() -> None:
    progress = AgentProgress.parse(valid_attempt() | {"progress": {"phase": "probe"}})
    result = AgentResult.parse(
        valid_attempt() | {"state": "succeeded", "result": {"healthy": True}}
    )

    assert progress.node_id == "spk_00000000000000000000000000000001"
    assert result.state == "succeeded"


def test_cancelled_result_is_a_typed_terminal_agent_state() -> None:
    raw = valid_attempt() | {
        "state": "cancelled",
        "result": {"reason": "controller cancellation requested"},
    }

    result = AgentResult.parse(raw)

    assert result.state == "cancelled"
    assert schema("agent-result.schema.json").is_valid(raw)
    assert validate_schema_message("agent-result.schema.json", raw).state == "cancelled"


def test_results_are_bounded_and_reject_secret_bearing_keys() -> None:
    with pytest.raises(AgentProtocolError, match="large"):
        AgentResult.parse(
            valid_attempt() | {"state": "succeeded", "result": {"x": "x" * 65536}}
        )
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(
            valid_attempt()
            | {"state": "succeeded", "result": {"private_key": "unsafe"}}
        )


def test_operation_enum_contains_only_supported_operations() -> None:
    assert {member.value for member in AgentOperation} == {
        "agent.upgrade.v1",
        "node.probe",
        "release.install",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.health",
        "workload.verify",
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.model-uninstall.v1",
    }


def test_removed_package_operation_strings_are_not_protocol_claims() -> None:
    payload = {
        "schema_version": 1,
        "deployment_id": "sample-package",
        "release_digest": "a" * 64,
        "deployment_digest": "b" * 64,
    }
    raw = claim_for_operation("package.prepare", payload)

    assert not schema("agent-job.schema.json").is_valid(raw)
    with pytest.raises(AgentProtocolError, match="operation"):
        AgentClaim.parse(raw)


def schema(name: str) -> Draft202012Validator:
    return schema_validator(name)


@pytest.mark.parametrize(
    ("name", "fixture"),
    [
        (
            "agent-job.schema.json",
            valid_claim() | {"payload": {"nested": {"apiToken": "unsafe"}}},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"payload": {"artifact_path": "release"}},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"payload": {"artifactPath": "release"}},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"payload": {"artifactPATH": "release"}},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"payload": {"artifactFILE": "release"}},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"payload": {"someDIRECTORY": "release"}},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"deadline": "2026-08-03T12:00:00+02:00"},
        ),
        (
            "agent-job.schema.json",
            valid_claim() | {"deadline": "2026-99-99T12:00:00+00:00"},
        ),
        (
            "agent-result.schema.json",
            valid_attempt()
            | {"state": "succeeded", "result": {"log_path": "/tmp/log"}},
        ),
        (
            "agent-result.schema.json",
            valid_attempt()
            | {
                "deadline": "2026-08-03T12:00:00+02:00",
                "state": "succeeded",
                "result": {},
            },
        ),
    ],
)
def test_schemas_reject_protocol_boundary_violations(
    name: str, fixture: dict[str, object]
) -> None:
    assert not schema(name).is_valid(fixture)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        (
            "agent-job.schema.json",
            valid_claim() | {"deadline": "2026-99-99T12:00:00+00:00"},
        ),
        (
            "agent-result.schema.json",
            valid_attempt()
            | {
                "deadline": "2026-99-99T12:00:00+00:00",
                "state": "succeeded",
                "result": {},
            },
        ),
    ],
)
def test_parse_and_shared_schema_validator_reject_bogus_utc_dates(
    name: str, raw: dict[str, object]
) -> None:
    parser = AgentClaim.parse if name == "agent-job.schema.json" else AgentResult.parse

    with pytest.raises(AgentProtocolError):
        parser(raw)
    with pytest.raises(AgentProtocolError):
        validate_schema_message(name, raw)


@pytest.mark.parametrize("name", ["agent-job.schema.json", "agent-result.schema.json"])
def test_shared_schema_validator_and_parser_reject_oversized_canonical_documents(
    name: str,
) -> None:
    document = {"x": "x" * 65536}
    if name == "agent-job.schema.json":
        raw = valid_claim() | {
            "payload": document,
            "payload_digest": hashlib.sha256(canonical_message(document)).hexdigest(),
        }
        parser = AgentClaim.parse
    else:
        raw = valid_attempt() | {"state": "succeeded", "result": document}
        parser = AgentResult.parse

    with pytest.raises(AgentProtocolError, match="large"):
        parser(raw)
    with pytest.raises(AgentProtocolError, match="large"):
        validate_schema_message(name, raw)


@pytest.mark.parametrize("name", ["agent-job.schema.json", "agent-result.schema.json"])
def test_packaged_schemas_match_repository_bytes(name: str) -> None:
    repository_schema = (
        Path(__file__).parents[1] / "src" / "vonk_agent_protocol" / "schemas" / name
    ).read_bytes()
    packaged_schema = (
        importlib.resources.files("vonk_agent_protocol") / "schemas" / name
    ).read_bytes()

    assert packaged_schema == repository_schema

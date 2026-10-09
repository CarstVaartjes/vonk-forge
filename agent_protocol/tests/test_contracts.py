from __future__ import annotations

import importlib.resources
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from vonk_agent_protocol import (
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES,
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    MAX_DOCUMENT_BYTES,
    AgentClaim,
    AgentEvidenceCode,
    AgentOperation,
    AgentProgress,
    AgentProtocolError,
    AgentResult,
    canonical_message,
    schema_validator,
    validate_result_for_operation,
    validate_schema_message,
)
from vonk_agent_protocol.contracts import RESULT_MODELS, _validate_safe_keys
from vonk_agent_protocol.package_upgrade import PackageActivationOutcome
from vonk_agent_protocol.recipe_operations import (
    RecipeStopPayload,
    RecipeUninstallPayload,
)


def valid_claim() -> dict[str, object]:
    compiled_plan = json.loads(
        (
            Path(__file__).parent / "fixtures" / "compiled-execution-plan-v2.json"
        ).read_text(encoding="utf-8")
    )
    payload = RecipeStopPayload(
        run_id="00000000-0000-4000-8000-000000000004",
        target_runtime_id="00000000-0000-4000-8000-000000000004",
        run_generation=1,
        installation_id="00000000-0000-4000-8000-000000000005",
        recipe_revision_id="00000000-0000-4000-8000-000000000006",
        mapping_id="00000000-0000-4000-8000-000000000007",
        plan_digest="a" * 64,
        rank=compiled_plan["runtime"]["placement"]["rank"],
        role=compiled_plan["runtime"]["placement"]["role"],
        recipe_content_sha256=compiled_plan["identity"]["recipe_revision_sha256"],
        stop_timeout_seconds=compiled_plan["lifecycle"]["stop_timeout_seconds"],
    ).model_dump(mode="json")
    return {
        "fence": "00000000-0000-4000-8000-000000000003",
        "operation": "recipe.stop",
        "payload": payload,
        "deadline": "2026-08-03T12:00:00+00:00",
    }


def valid_attempt() -> dict[str, object]:
    return {"fence": valid_claim()["fence"]}


def claim_with_payload(payload: dict[str, str]) -> dict[str, object]:
    current = valid_claim()
    return claim_for_operation("recipe.stop", current["payload"] | payload)


def claim_for_operation(
    operation: str, payload: dict[str, object]
) -> dict[str, object]:
    return valid_claim() | {"operation": operation, "payload": payload}


def recipe_build_vectors() -> dict[str, object]:
    return json.loads(
        (
            importlib.resources.files("vonk_agent_protocol")
            / "vectors"
            / "recipe-build-claim-v1.json"
        ).read_text(encoding="utf-8")
    )


def recipe_start_result(
    *, endpoint: str = "http://[fd00::211]:8000"
) -> dict[str, object]:
    return {"endpoint": endpoint}


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

    assert json.loads(canonical_message(claim))["operation"] == "recipe.stop"


@pytest.mark.parametrize("field", ["command", "shell", "environment", "password"])
def test_protocol_rejects_execution_and_secret_fields(field: str) -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {field: "unsafe"}})


def test_protocol_rejects_unsafe_keys_recursively() -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(valid_claim() | {"payload": {"safe": {"apiToken": "unsafe"}}})


def test_protocol_allows_only_exact_versioned_platform_target_identifier() -> None:
    target = "platform/releases/1.2.3/" + "a" * 64 + ".json"

    _validate_safe_keys({"platform_target_name": target})


@pytest.mark.parametrize(
    "target",
    (
        "platform-release.json",
        "platform/releases/latest/" + "a" * 64 + ".json",
        "platform/releases/1.2.3/../../escape.json",
        "platform/releases/1.2.3/" + "A" * 64 + ".json",
    ),
)
def test_protocol_rejects_client_selected_parameter_path(
    target: str,
) -> None:
    with pytest.raises(AgentProtocolError, match="unsafe|path"):
        AgentClaim.parse(claim_with_payload({"artifact_path": target}))


@pytest.mark.parametrize(
    "deadline",
    ["2026-08-03T12:00:00", "2026-08-03T12:00:00+02:00"],
)
def test_claim_requires_an_aware_utc_deadline(deadline: str) -> None:
    with pytest.raises(AgentProtocolError, match="deadline"):
        AgentClaim.parse(valid_claim() | {"deadline": deadline})


def test_claim_copies_canonical_payload_before_becoming_frozen() -> None:
    payload = valid_claim()["payload"]
    assert isinstance(payload, dict)
    source = valid_claim() | {
        "payload": payload | {"run_id": "00000000-0000-4000-8000-000000000005"}
    }
    claim = AgentClaim.parse(source)
    source["payload"]["run_id"] = "00000000-0000-4000-8000-000000000006"  # type: ignore[index]

    assert (
        json.loads(canonical_message(claim))["payload"]["run_id"]
        == "00000000-0000-4000-8000-000000000005"
    )
    with pytest.raises(ValidationError):
        claim.fence = "00000000-0000-4000-8000-000000000009"  # type: ignore[misc]


def test_direct_construction_cannot_bypass_claim_validation_or_serialization() -> None:
    raw = valid_claim()

    with pytest.raises(ValidationError, match="unsafe"):
        AgentClaim(
            fence=raw["fence"],
            operation=AgentOperation.RECIPE_STOP,
            payload={"command": "unsafe"},
            deadline=datetime(2026, 8, 3, 12, tzinfo=UTC),
        )


def test_direct_result_construction_rejects_client_filesystem_paths() -> None:
    raw = valid_attempt()

    with pytest.raises(ValidationError):
        AgentResult(
            **raw,
            state="succeeded",
            result={"evidence": "/var/lib/vonk-agent/result.json"},
        )


def test_recipe_start_result_accepts_only_a_typed_endpoint_uri() -> None:
    parsed = AgentResult.parse(
        valid_attempt() | {"state": "succeeded", "result": recipe_start_result()}
    )

    assert parsed.result["endpoint"] == "http://[fd00::211]:8000"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "/var/lib/vonk-forge/result.json"),
        ("endpoint", "http://192.168.1.211:8000/private"),
        ("endpoint", "http://worker.example.invalid:8000"),
        ("endpoint", "http://192.168.1.211:0"),
    ],
)
def test_typed_recipe_result_uri_fields_reject_path_or_credential_confusion(
    field: str, value: str
) -> None:
    result = recipe_start_result() | {field: value}

    with pytest.raises(AgentProtocolError, match="path|credential|endpoint"):
        AgentResult.parse(valid_attempt() | {"state": "succeeded", "result": result})


def test_typed_result_uri_exceptions_do_not_apply_to_claims_or_progress() -> None:
    endpoint = "http://192.168.1.211:8000"

    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(claim_with_payload({"endpoint": endpoint}))
    # Progress is optional evidence: an invalid document is dropped and named,
    # so the lease heartbeat it rides on is never refused.
    progress = AgentProgress(**valid_attempt(), progress={"endpoint": endpoint})
    assert progress.progress is None
    assert progress.evidence_warnings == (AgentEvidenceCode.PROGRESS_DROPPED,)


def test_direct_progress_construction_enforces_protocol_boundary() -> None:
    raw = valid_attempt()

    progress = AgentProgress(**raw, progress={"authorization": "unsafe"})
    assert progress.progress is None
    assert progress.evidence_warnings == (AgentEvidenceCode.PROGRESS_DROPPED,)


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

    claim = AgentClaim.parse(claim_for_operation("recipe.build.v1", payload))

    assert claim.payload["dockerfile"] == "containers/runtime/Dockerfile"
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
        {"dockerfile": "/etc/passwd"},
        {"dockerfile": "../Dockerfile"},
        {"dockerfile": "containers//Dockerfile"},
        {"base_images": [{"reference": "ghcr.io/vonkforge/runtime:latest"}]},
        {"evidence": "host/path"},
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
        "source_package_bytes": 4_000_000,
        "source_package_url": "https://install.vonkforge.ai/artifacts/agent-packages/"
        + "a" * 64
        + "/vonk-forge-agent.deb",
        "rollback": {
            "source": {
                "package_sha256": "a" * 64,
                "package_signature": "b" * 128,
                "package_version": "0.1.0~dev.329+g0123456789ab",
                "binary_sha256": "c" * 64,
                "helper_sha256": "d" * 64,
            },
            "attempt_nonce": "f" * 64,
            "activation_deadline": 2_000_000_000,
        },
    }
    raw = claim_for_operation("agent.upgrade.v1", payload)

    assert AgentClaim.parse(raw)
    assert schema("agent-job.schema.json").is_valid(raw)
    assert validate_schema_message("agent-job.schema.json", raw)


def test_agent_upgrade_success_uses_the_current_typed_result() -> None:
    result = {
        "architecture": "linux-arm64",
        "binary_digest": "d" * 64,
        "build_digest": "sha256:" + "e" * 64,
        "package_sha256": "b" * 64,
        "package_version": "0.1.0~dev.330+g0123456789ab",
        "status": "upgraded",
        "activation_receipt": {
            "schema_version": 2,
            "node_id": "spk_" + "1" * 32,
            "source_package_sha256": "a" * 64,
            "source_version": "0.1.0~dev.329+g0123456789ab",
            "source_binary_sha256": "c" * 64,
            "candidate_package_sha256": "b" * 64,
            "candidate_version": "0.1.0~dev.330+g0123456789ab",
            "candidate_binary_sha256": "d" * 64,
            "attempt_nonce": "f" * 64,
            "phase": "acknowledged",
            "created_at": 100,
            "updated_at": 130,
            "outcome": PackageActivationOutcome.CONTROLLER_CONFIRMED_ACTIVATION,
        },
    }

    parsed = validate_result_for_operation(
        AgentOperation.AGENT_UPGRADE,
        result,
        state="succeeded",
    )

    assert parsed is not None
    assert parsed.__class__.__name__ == "AgentUpgradeResult"
    with pytest.raises(AgentProtocolError, match="typed model"):
        validate_result_for_operation(
            AgentOperation.AGENT_UPGRADE,
            result | {"status": "failed"},
            state="succeeded",
        )


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

    with pytest.raises(AgentProtocolError, match="path|unsafe"):
        parser(raw)
    if name == "agent-result.schema.json":
        assert not schema(name).is_valid(raw)
        with pytest.raises(AgentProtocolError):
            validate_schema_message(name, raw)


@pytest.mark.parametrize("name", ["agent-job.schema.json"])
@pytest.mark.parametrize("field", SAFE_PATH_KEY_COLLISIONS)
def test_safe_key_scanner_preserves_path_token_collisions(
    name: str,
    field: str,
) -> None:
    _validate_safe_keys({field: "release"})


def test_progress_and_result_are_fenced_messages() -> None:
    progress = AgentProgress.parse(valid_attempt() | {"progress": {"phase": "probe"}})
    result = AgentResult.parse(valid_attempt() | {"state": "succeeded", "result": {}})

    assert progress.fence == result.fence
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


def test_results_reject_secret_bearing_keys() -> None:
    with pytest.raises(AgentProtocolError):
        AgentResult.parse(
            valid_attempt()
            | {"state": "succeeded", "result": {"private_key": "unsafe"}}
        )


def test_operation_enum_contains_only_supported_operations() -> None:
    assert {member.value for member in AgentOperation} == {
        "agent.upgrade.v1",
        "runtime.preflight.v1",
        "artifact.distribution.v1",
        "recipe.build.v1",
        "recipe.build.cleanup.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.reconcile",
    }


def test_removed_package_operation_strings_are_not_protocol_claims() -> None:
    payload = {
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
    maximum = (
        MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
        if name == "agent-job.schema.json"
        else MAX_DOCUMENT_BYTES
    )
    document = {"x": "x" * (maximum + 1)}
    if name == "agent-job.schema.json":
        raw = valid_claim() | {"payload": document}
        parser = AgentClaim.parse
    else:
        raw = valid_attempt() | {"state": "succeeded", "result": document}
        parser = AgentResult.parse

    with pytest.raises(AgentProtocolError, match="large"):
        parser(raw)
    with pytest.raises(AgentProtocolError, match="large"):
        validate_schema_message(name, raw)


@pytest.mark.parametrize(
    "operation",
    [
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_UNINSTALL,
    ],
)
def test_authenticated_recipe_launch_claims_have_dedicated_document_ceiling(
    operation: str,
) -> None:
    compiled_plan = json.loads(
        (
            Path(__file__).parents[2]
            / "control"
            / "tests"
            / "fixtures"
            / "compiled_plan_751.json"
        ).read_text(encoding="utf-8")
    )
    if operation == AgentOperation.RECIPE_INSTALL:
        corpus_payload = {
            "installation_id": "00000000-0000-4000-8000-000000000004",
            "plan_digest": "b" * 64,
            "expected_bytes": sum(
                artifact["size_bytes"] for artifact in compiled_plan["artifacts"]
            ),
            "compiled_execution_plan": compiled_plan,
        }
    elif operation == AgentOperation.RECIPE_START:
        compiled_plan = deepcopy(compiled_plan)
        compiled_plan["runtime"]["placement"]["endpoint_address"] = "10.0.0.2"
        compiled_plan["security"]["network_mode"] = "bridge"
        corpus_payload = {
            "run_id": "00000000-0000-4000-8000-000000000005",
            "installation_id": "00000000-0000-4000-8000-000000000004",
            "recipe_revision_id": "00000000-0000-4000-8000-000000000006",
            "mapping_id": "00000000-0000-4000-8000-000000000007",
            "run_generation": 1,
            "plan_digest": "b" * 64,
            "compiled_execution_plan": compiled_plan,
        }

    else:
        corpus_payload = json.loads(
            canonical_message(
                RecipeUninstallPayload.model_validate_json(
                    json.dumps(
                        {
                            "installation_id": "00000000-0000-4000-8000-000000000004",
                            "recipe_content_sha256": "a" * 64,
                            "plan_digest": "b" * 64,
                            "cleanup_model_content_sha256": None,
                            "compiled_execution_plan": compiled_plan,
                        }
                    )
                )
            )
        )

    claim = AgentClaim.parse(claim_for_operation(operation, corpus_payload))
    assert claim.operation.value == operation

    oversized_plan = deepcopy(compiled_plan)
    oversized_plan["runtime"]["executable"] = (
        "/" + "x" * MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
    )
    oversized = deepcopy(corpus_payload)
    oversized["compiled_execution_plan"] = oversized_plan
    with pytest.raises(AgentProtocolError, match="large|at most"):
        AgentClaim.parse(claim_for_operation(operation, oversized))

    with pytest.raises(AgentProtocolError, match="large"):
        AgentClaim.parse(
            claim_for_operation(
                "recipe.stop",
                {"value": "x" * MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES},
            )
        )


@pytest.mark.parametrize("name", ["agent-job.schema.json", "agent-result.schema.json"])
def test_core_schemas_are_derived_from_the_registry(name: str) -> None:
    validator = schema_validator(name)
    assert validator.schema["additionalProperties"] is False
    assert "fence" in validator.schema["required"]


def test_known_operation_result_uses_its_typed_result_model() -> None:
    parsed = validate_result_for_operation(
        AgentOperation.RECIPE_STOP, {}, state="succeeded"
    )
    assert parsed is not None
    with pytest.raises(AgentProtocolError, match="typed model"):
        validate_result_for_operation(
            AgentOperation.RECIPE_STOP, {"stopped": True}, state="succeeded"
        )


def test_every_current_operation_has_a_result_model() -> None:
    assert set(RESULT_MODELS) == set(AgentOperation)


def test_distribution_result_cannot_fall_through_to_generic_evidence() -> None:
    complete = {"downloaded_bytes": 1024}
    parsed = validate_result_for_operation(
        AgentOperation.ARTIFACT_DISTRIBUTION,
        complete,
        state="succeeded",
    )
    assert parsed is not None
    with pytest.raises(AgentProtocolError, match="typed model"):
        validate_result_for_operation(
            AgentOperation.ARTIFACT_DISTRIBUTION,
            complete | {"downloaded_bytes": "1024"},
            state="succeeded",
        )


def test_lease_only_progress_omission_and_null_have_identical_canonical_bytes() -> None:
    omitted = AgentProgress.model_validate(valid_attempt())
    explicit = AgentProgress.model_validate(valid_attempt() | {"progress": None})
    assert omitted.progress is None
    assert canonical_message(omitted) == canonical_message(explicit)
    assert "progress" not in json.loads(canonical_message(omitted))
    measured = AgentProgress.model_validate(
        valid_attempt() | {"progress": {"phase": "queued"}}
    )
    assert json.loads(canonical_message(measured))["progress"] == {
        "phase": "queued",
        "completed_bytes": 0,
        "total_bytes_known": False,
        "members": [],
    }


def test_uninstall_claim_accepts_typed_plan_and_rejects_untyped_paths() -> None:
    """Cleanup uses the canonical artifact paths; arbitrary host paths stay refused."""
    plan = json.loads(
        (Path(__file__).parent / "fixtures/compiled-execution-plan-v2.json").read_text()
    )
    payload = RecipeUninstallPayload.model_validate_json(
        json.dumps(
            {
                "installation_id": "00000000-0000-4000-8000-000000000004",
                "recipe_content_sha256": "a" * 64,
                "plan_digest": "b" * 64,
                "cleanup_model_content_sha256": None,
                "compiled_execution_plan": plan,
            }
        )
    )
    claim = AgentClaim.parse(
        claim_for_operation(
            AgentOperation.RECIPE_UNINSTALL, json.loads(canonical_message(payload))
        )
    )
    assert claim.payload == payload
    unsafe = json.loads(canonical_message(payload))
    unsafe["host_path"] = "/etc/passwd"
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(claim_for_operation(AgentOperation.RECIPE_UNINSTALL, unsafe))

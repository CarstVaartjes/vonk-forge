from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from cluster_profiles.fleet_qualification import ArtifactJobSmokeAdapter
from cluster_profiles.qualification_fixtures import (
    Fixture,
    FixtureRegistry,
    RecipeFixture,
    _parse_assertion,
    _parse_recipe_fixture,
    _safe_zip_entries,
    _validate_document_archive,
    _validate_magic,
    _validate_realtime_transcript,
    _validate_synchronized_media_receipt,
    validate_outputs,
)
from control.tests.consumer_outcomes import not_adopted
from tests.cluster_profiles.test_glb_validation import Glb

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAACXBIWXMAAAABAAAAAQBPJcTWAAAAYElEQVR4nO3PwQkAIBDAMAX3H/lwCB9BaCZo96y/HR3wqgGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQHtAgK6AfwYG1VIAAAAAElFTkSuQmCC"
)


def _registry() -> FixtureRegistry:
    prompt_content = b"Draw a red square.\n"
    prompt = Fixture(
        "prompt",
        "prompt.txt",
        "identity",
        "prompt.txt",
        "text/plain",
        len(prompt_content),
        hashlib.sha256(prompt_content).hexdigest(),
        prompt_content,
    )
    recipe = RecipeFixture(
        "vonk-forge/image",
        "a" * 64,
        "image-job",
        {},
        (("prompt", prompt),),
        {
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 1024,
            "allowed_media_types": ["image/png"],
        },
        60,
        (
            {"kind": "file-count", "exact": 1},
            {"kind": "media-type", "allowed": ["image/png"]},
            {"kind": "format", "format": "png"},
        ),
    )
    return FixtureRegistry(
        {"prompt": prompt},
        {recipe.key: recipe},
        {},
    )


def test_glb_fixture_validation_rejects_header_only_transport_stub() -> None:
    with not_adopted():
        _validate_magic(b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00", "glb")
    repaired = Glb()
    _validate_magic(repaired.bytes(repaired.document()), "glb")


def test_registry_reuses_content_without_provenance_and_ignores_unused_members(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "pixel.png"
    asset.write_bytes(PNG)
    fixture = {
        "path": "pixel.png",
        "encoding": "identity",
        "name": "pixel.png",
        "media_type": "image/png",
        "size_bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    document = {
        "schema_version": 2,
        "fixtures": {"unused": fixture, "broken": None},
        "recipes": {},
        "special_fixtures": {},
        "service_case_templates": {},
        "service_recipes": {},
    }
    manifest = tmp_path / "fixtures.json"
    manifest.write_text(json.dumps(document))
    registry = FixtureRegistry.load(manifest)
    assert registry.fixtures["unused"].content == PNG
    fixture["provenance"] = {"damaged": "annotation"}
    manifest.write_text(json.dumps(document))
    assert FixtureRegistry.load(manifest).fixtures["unused"].content == PNG


def test_registry_isolates_damaged_member_and_repairs_on_the_same_resolver(tmp_path):
    asset = tmp_path / "pixel.png"
    asset.write_bytes(PNG)
    fixture = {
        "path": "pixel.png",
        "encoding": "identity",
        "name": "pixel.png",
        "media_type": "image/png",
        "size_bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(),
    }
    recipe = {
        "content_sha256": "a" * 64,
        "interface": "image-job",
        "parameters": {},
        "inputs": [{"slot": "image", "fixture": "image"}],
        "output_limits": {
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 1024,
            "allowed_media_types": ["image/png"],
        },
        "timeout_seconds": 60,
        "assertions": [{"kind": "image-metadata", "width": 64, "height": 64}],
    }
    document = {
        "schema_version": 2,
        "fixtures": {"image": fixture, "bad": None},
        "recipes": {"vonk-forge/image": recipe, "vonk-forge/bad": None},
        "special_fixtures": {},
        "service_case_templates": {},
        "service_recipes": {},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document))
    registry = FixtureRegistry.load(path)
    assert registry.resolve("vonk-forge/image", "a" * 64, "image-job")[0] is not None
    asset.write_bytes(b"damaged")
    assert registry.resolve("vonk-forge/image", "a" * 64, "image-job")[0] is None
    asset.write_bytes(PNG)
    repaired = registry.resolve("vonk-forge/image", "a" * 64, "image-job")[0]
    assert repaired is not None and repaired.inputs[0][1].content == PNG
    assert registry.resolve("vonk-forge/bad", "a" * 64, "image-job")[0] is None
    assert registry.resolve("vonk-forge/image", "a" * 64, "image-job")[0] is not None


def test_assertion_parser_rejects_unknown_fields_and_missing_semantics() -> None:
    with not_adopted():
        _parse_assertion(
            "vonk-forge/image",
            {"kind": "image-metadata", "width": 64, "height": 64, "typo": 1},
        )

    registry = _registry()
    with not_adopted():
        _parse_recipe_fixture(
            "vonk-forge/image",
            {
                "content_sha256": "a" * 64,
                "interface": "image-job",
                "parameters": {},
                "inputs": [{"slot": "prompt", "fixture": "prompt"}],
                "output_limits": {
                    "max_files": 1,
                    "max_file_bytes": 1024,
                    "max_total_bytes": 1024,
                    "allowed_media_types": ["image/png"],
                },
                "timeout_seconds": 60,
                "assertions": [{"kind": "file-count", "exact": 1}],
            },
            registry.fixtures,
        )
    assert (
        _parse_assertion(
            "vonk-forge/image", {"kind": "image-metadata", "width": 64, "height": 64}
        )["width"]
        == 64
    )


def _ocr_zip(*, characters_delta: int = 0, extra_name: str | None = None) -> bytes:
    markdown = "# OCR\n\n7\n"
    manifest = {
        "documents": [
            {
                "characters": len(markdown) + characters_delta,
                "early_stopped_tail_repetition": False,
                "input": "digit7.png",
                "output": "documents/001-digit7.md",
            }
        ],
        "inference": "vllm-dflash",
        "model": "example/document-model",
        "model_revision": "a" * 40,
        "runtime_source_revision": "b" * 40,
        "sampling": {
            "repetition_penalty": 1.08,
            "temperature": 0.0,
            "top_k": -1,
            "top_p": 1.0,
        },
        "schema_version": 1,
        "task_type": "doc_parse",
    }
    destination = io.BytesIO()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("documents/001-digit7.md", markdown)
        if extra_name is not None:
            archive.writestr(extra_name, "unsafe")
    return destination.getvalue()


def test_document_archive_is_closed_and_semantic() -> None:
    assertion = {
        "exact_names": ["manifest.json", "documents/001-digit7.md"],
        "manifest_equals": {
            "inference": "vllm-dflash",
            "model": "example/document-model",
            "model_revision": "a" * 40,
            "runtime_source_revision": "b" * 40,
            "schema_version": 1,
            "task_type": "doc_parse",
        },
        "sampling_equals": {
            "repetition_penalty": 1.08,
            "temperature": 0.0,
            "top_k": -1,
            "top_p": 1.0,
        },
        "input_name": "digit7.png",
        "output_name": "documents/001-digit7.md",
        "text_pattern": r"(?<!\d)7(?!\d)",
    }
    _validate_document_archive(_ocr_zip(), assertion)
    with not_adopted():
        _validate_document_archive(_ocr_zip(characters_delta=1), assertion)
    with not_adopted():
        _safe_zip_entries(_ocr_zip(extra_name="../escaped"))
    _validate_document_archive(_ocr_zip(), assertion)


def test_realtime_transcript_requires_authority_ack_and_terminal_record() -> None:
    records = [
        {
            "sequence": 0,
            "elapsed_seconds": 0.0,
            "type": "session-start",
            "model_revision": "c" * 40,
        },
        {
            "sequence": 1,
            "elapsed_seconds": 0.1,
            "type": "frame-ack",
            "event_index": 0,
            "timestamp": 0.0,
            "dropped_oldest": False,
        },
        {"sequence": 2, "elapsed_seconds": 1.0, "type": "session-stop"},
    ]
    content = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n" for record in records
    )
    assertion = {"model_revision": "c" * 40, "frame_count": 1}
    _validate_realtime_transcript(content, assertion)

    corrupted = content.replace(b'"event_index":0', b'"event_index":1')
    with not_adopted():
        _validate_realtime_transcript(corrupted, assertion)
    _validate_realtime_transcript(content, assertion)


def test_synchronized_media_receipt_uses_only_declared_expectations() -> None:
    output = b"synthetic media"
    assertion = {
        "output_name": "result.bin",
        "allowed_profiles": ["portable"],
        "media_equals": {"frames": 2},
        "media_positive_integers": ["samples"],
        "runtime_equals": {"source_revision": "r1"},
        "runtime_nullable_strings": ["accelerator"],
        "runtime_nullable_integers": ["driver"],
        "runtime_nonempty_strings": ["framework"],
        "tensor_shapes": {"audio": None, "video": [2, 4, 4, 3]},
    }
    document = {
        "media": {"frames": 2, "samples": 8},
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "profile": "portable",
        "prompt_sha256": "d" * 64,
        "runtime": {
            "source_revision": "r1",
            "accelerator": None,
            "driver": 1,
            "framework": "example",
        },
        "seed": 7,
        "tensors": {
            "audio": {"dtype": "float32", "shape": [1, 8], "sha256": "e" * 64},
            "video": {
                "dtype": "float32",
                "shape": [2, 4, 4, 3],
                "sha256": "f" * 64,
            },
        },
    }
    content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    _validate_synchronized_media_receipt(
        content + b"\n", {"result.bin": output}, "portable", assertion
    )

    document["tensors"]["video"]["shape"] = [1, 4, 4, 3]
    corrupted = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    with not_adopted():
        _validate_synchronized_media_receipt(
            corrupted + b"\n", {"result.bin": output}, "portable", assertion
        )
    _validate_synchronized_media_receipt(
        content + b"\n", {"result.bin": output}, "portable", assertion
    )


def test_registry_fails_closed_for_missing_changed_and_special_fixtures() -> None:
    registry = _registry()

    recipe, blocker = registry.resolve("vonk-forge/image", "a" * 64, "image-job")
    assert recipe is not None and blocker is None
    _recipe, digest_blocker = registry.resolve(
        "vonk-forge/image", "c" * 64, "image-job"
    )
    assert digest_blocker is not None
    assert _recipe is None
    assert registry.resolve("vonk-forge/image", "a" * 64, "image-job")[0] is recipe
    _recipe, missing_blocker = registry.resolve(
        "vonk-forge/missing", "a" * 64, "image-job"
    )
    assert missing_blocker is not None
    assert _recipe is None
    assert registry.resolve("vonk-forge/image", "a" * 64, "image-job")[0] is recipe


class _DownloadClient:
    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]:
        del path
        assert media_type in {"application/octet-stream", "image/png"}
        assert expected_sha256 == hashlib.sha256(PNG).hexdigest()
        assert expected_size == len(PNG)
        assert overwrite is False
        destination.write_bytes(PNG)
        return {"sha256": expected_sha256}


def test_output_assertions_download_and_validate_exact_artifacts() -> None:
    recipe = _registry().recipes["vonk-forge/image"]
    digest = hashlib.sha256(PNG).hexdigest()
    result = {
        "id": "job-1",
        "output_manifest_sha256": "c" * 64,
        "output_files": [
            {
                "name": "result.png",
                "media_type": "image/png",
                "size_bytes": len(PNG),
                "sha256": digest,
            }
        ],
    }

    evidence = validate_outputs(recipe, result, _DownloadClient())

    output_files = evidence["output_files"]
    assert isinstance(output_files, list)
    assert output_files[0]["sha256"] == digest
    assert evidence["output_manifest_sha256"] == "c" * 64


def test_output_assertions_reject_mime_mismatch() -> None:
    recipe = _registry().recipes["vonk-forge/image"]
    with not_adopted():
        validate_outputs(
            recipe,
            {
                "id": "job-1",
                "output_files": [
                    {
                        "name": "result.bin",
                        "media_type": "application/octet-stream",
                        "size_bytes": len(PNG),
                        "sha256": hashlib.sha256(PNG).hexdigest(),
                    }
                ],
            },
            _DownloadClient(),
        )


class _ArtifactClient(_DownloadClient):
    """Produces the Controller's current response contract, including receipts."""

    def __init__(self):
        self.calls = []
        self.status_reads = {}
        self.uploaded = []
        self.request_ids = []
        self.created = 0
        self.jobs = {}
        self.accepted = {}

    def _response(self, job_id, *, state=None, preparation=None):
        from cluster_profiles.control_client import validate_control_document
        from cluster_profiles.generated_control.models.artifact_job_response import (
            ArtifactJobResponse,
        )

        body = self.jobs[job_id]
        value = {
            "id": job_id,
            "run_id": "11111111-1111-4111-8111-111111111111",
            "interface": body["interface"],
            "state": state,
            "preparation": preparation,
            "compiled_contract": None,
            "contract_sha256": "d" * 64,
            "input_manifest_sha256": "e" * 64,
            "input_total_bytes": sum(item["size_bytes"] for item in body["inputs"]),
            "input_declarations": body["inputs"],
            "input_files": body["inputs"],
            "output_limits": body["output_limits"],
            "timeout_seconds": body["timeout_seconds"],
            "output_files": [],
            "created_at": "2026-10-09T00:00:00Z",
            "updated_at": "2026-10-09T00:00:00Z",
        }
        if state is not None:
            value.update(operation_id=job_id, submit_request_id=self.accepted[job_id])
        if state == "succeeded":
            value.update(
                output_manifest_sha256="f" * 64,
                result_evidence={},
                output_files=[
                    {
                        "name": "result.png",
                        "media_type": "image/png",
                        "size_bytes": len(PNG),
                        "sha256": hashlib.sha256(PNG).hexdigest(),
                    }
                ],
            )
        return ArtifactJobResponse.from_dict(
            validate_control_document("ArtifactJobResponse", value)
        ).to_dict()

    def request(
        self,
        method,
        path,
        payload=None,
        *,
        extra_headers=None,
        query=None,
        timeout_seconds=None,
    ):
        self.calls.append((method, path))
        if path.endswith("/artifact-jobs"):
            assert extra_headers is not None
            key = extra_headers["X-Request-ID"]
            self.request_ids.append(key)
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
            if job_id not in self.jobs:
                self.created += 1
                self.jobs[job_id] = payload
            return (
                self._response(job_id, state="succeeded")
                if job_id in self.accepted
                else self._response(job_id, preparation="draft")
            )
        if path.startswith("/api/artifact-jobs/requests/"):
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, path.rsplit("/", 1)[1]))
            return self._response(job_id, preparation="draft")
        if method == "GET":
            job_id = path.rsplit("/", 1)[1]
            self.status_reads[job_id] = self.status_reads.get(job_id, 0) + 1
            return self._response(job_id, state="succeeded")
        job_id = path.split("/")[-2]
        if path.endswith("/finalize"):
            return self._response(job_id, preparation="ready")
        if path.endswith("/submit"):
            assert extra_headers is not None
            self.accepted[job_id] = extra_headers["X-Request-ID"]
            return self._response(job_id, state="queued")
        raise AssertionError((method, path))

    def upload_file(self, path, source, *, media_type, expected_sha256, expected_size):
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_sha256
        assert len(source.read_bytes()) == expected_size
        self.uploaded.append(path)
        return self._response(path.split("/")[-3], preparation="draft")


def test_artifact_adapter_runs_each_digest_bound_case_as_its_own_job(tmp_path) -> None:
    base_registry = _registry()
    primary = base_registry.recipes["vonk-forge/image"]
    recipe = replace(
        primary, supplemental_cases=(replace(primary, case_id="alternate"),)
    )
    registry = FixtureRegistry(
        base_registry.fixtures,
        {recipe.key: recipe},
        {},
    )
    client = _ArtifactClient()

    result = ArtifactJobSmokeAdapter(registry, request_directory=tmp_path).run(
        client,
        "11111111-1111-4111-8111-111111111111",
        recipe_key=recipe.key,
        recipe_content_sha256=recipe.content_sha256,
        interface=recipe.interface,
        timeout_seconds=300,
        poll_interval_seconds=0.1,
        clock=lambda: 0,
        sleeper=lambda _seconds: None,
    )

    cases = result["cases"]
    assert isinstance(cases, list)
    assert [case["case_id"] for case in cases] == ["default", "alternate"]
    assert len({case["job_id"] for case in cases}) == 2
    assert all(
        case["output_files"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
        for case in cases
    )
    assert len(set(client.request_ids)) == 2
    assert len(client.uploaded) == 2


def test_artifact_smoke_reconciles_acceptance_and_missing_output_then_reuses_after_restart(
    tmp_path,
):
    from cluster_profiles.control_client import ControlTransportError

    class LostReply(_ArtifactClient):
        lost = False
        missing = False

        def request(self, method, path, payload=None, **kwargs):
            value = super().request(method, path, payload, **kwargs)
            if path.endswith("/artifact-jobs") and not self.lost:
                self.lost = True
                raise ControlTransportError("accepted reply lost")
            if method == "GET" and "/requests/" not in path and not self.missing:
                self.missing = True
                value["output_files"] = []
            return value

    registry = _registry()
    client = LostReply()
    ticks = [0.0]

    def sleep(seconds):
        ticks[0] += seconds

    def run(scope="original"):
        result = ArtifactJobSmokeAdapter(
            registry, request_directory=tmp_path, request_scope=scope
        ).run(
            client,
            "11111111-1111-4111-8111-111111111111",
            recipe_key="vonk-forge/image",
            recipe_content_sha256="a" * 64,
            interface="image-job",
            timeout_seconds=10,
            poll_interval_seconds=0.1,
            clock=lambda: ticks[0],
            sleeper=sleep,
        )
        cases = result["cases"]
        assert isinstance(cases, list)
        return cases

    first = run()
    assert first[0]["output_files"][0]["sha256"] == hashlib.sha256(PNG).hexdigest()
    fresh = run()
    assert fresh[0]["job_id"] != first[0]["job_id"] and client.created == 2
    independent = run("fresh")
    assert independent[0]["job_id"] != fresh[0]["job_id"] and client.created == 3


@pytest.mark.parametrize("uncertain", [False, True])
def test_artifact_timeout_observes_cancel_identity_and_admits_fresh_case(
    tmp_path, uncertain
):
    class Pending(_ArtifactClient):
        cancelled = False
        cancel_key = None

        def request(self, method, path, payload=None, **kwargs):
            if path.endswith("/cancel"):
                self.cancelled = True
                self.cancel_key = kwargs["extra_headers"]["X-Request-ID"]
                job_id = path.split("/")[-2]
                value = self._response(job_id, state="cancelled")
                value["result_evidence"] = {
                    "cancel_request_id": self.cancel_key,
                    "active_scope_may_remain": uncertain,
                }
                return value
            value = super().request(method, path, payload, **kwargs)
            if method == "GET" and "/requests/" not in path:
                job_id = path.rsplit("/", 1)[1]
                value = self._response(
                    job_id, state="cancelled" if self.cancelled else "running"
                )
                if self.cancelled:
                    value["result_evidence"] = {
                        "cancel_request_id": self.cancel_key,
                        "active_scope_may_remain": uncertain,
                    }
            return value

    client = Pending()
    ticks = [0.0]

    def sleep(seconds):
        ticks[0] += seconds

    def run():
        result = ArtifactJobSmokeAdapter(_registry(), request_directory=tmp_path).run(
            client,
            "11111111-1111-4111-8111-111111111111",
            recipe_key="vonk-forge/image",
            recipe_content_sha256="a" * 64,
            interface="image-job",
            timeout_seconds=1,
            poll_interval_seconds=0.1,
            clock=lambda: ticks[0],
            sleeper=sleep,
        )
        cases = result["cases"]
        assert isinstance(cases, list)
        return cases

    ended = run()[0]
    assert ended["cleanup_confirmed"] is (not uncertain)
    assert ticks[0] <= 2.1 and client.created == 1
    client.cancelled = False
    fresh = run()[0]
    assert fresh["job_id"] != ended["job_id"] and client.created == 2


@pytest.mark.parametrize(
    "unreadable",
    [b"unreadable", b'{"choices":[{"message":{"content":"hello"}}]}'],
)
def test_serving_unknown_reobserves_then_quality_is_measured_and_new_case_runs(
    monkeypatch,
    unreadable,
):
    import io

    from cluster_profiles import serving_execution

    ticks = [0.0]
    monkeypatch.setattr(serving_execution.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        serving_execution.time,
        "sleep",
        lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
    )
    responses = iter(
        [
            unreadable,
            json.dumps(
                {
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {"completion_tokens": 1},
                }
            ).encode(),
        ]
    )
    requested = []

    class Reply(io.BytesIO):
        status = 200

        def __init__(self, content):
            super().__init__(content)
            self.headers = {}

    def open_reply(request, timeout):
        requested.append(timeout)
        return Reply(next(responses))

    check = {
        "kind": "openai.chat",
        "request": {
            "method": "POST",
            "path": "/chat/completions",
            "body": {"max_tokens": 5},
        },
        "assertions": ["chat.nonempty", "chat.output-cap"],
    }
    result = serving_execution.execute_http_check(
        "https://example.invalid", check, timeout_seconds=1, opener=open_reply
    )
    assert result["choices"] == 1 and requested == [1, 0.5]
    with not_adopted():
        serving_execution.evaluate_http_response(
            serving_execution.HttpObservation(
                200, {}, b'{"choices":[{"message":{"content":""}}]}'
            ),
            check,
        )
    fresh = serving_execution.execute_http_check(
        "https://example.invalid",
        check,
        timeout_seconds=1,
        opener=lambda request, timeout: Reply(
            b'{"choices":[{"message":{"content":"restored"}}],"usage":{"completion_tokens":1}}'
        ),
    )
    assert fresh["choices"] == 1


def test_service_quality_failure_is_scoped_and_unknown_reply_reobserves(monkeypatch):
    import io

    from cluster_profiles import fleet_qualification

    ticks = [0.0]
    monkeypatch.setattr(fleet_qualification.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        fleet_qualification.time,
        "sleep",
        lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
    )

    class Reply(io.BytesIO):
        status = 200

    responses = iter([b"broken", b'{"value":0}', b'{"value":1}'])
    adapter = fleet_qualification.ServiceSmokeAdapter(
        _registry(), opener=lambda request, timeout: Reply(next(responses))
    )
    case = {
        "id": "one",
        "method": "GET",
        "path": "/health",
        "body": None,
        "max_response_bytes": 1024,
        "timeout_seconds": 1,
        "assertions": [{"kind": "path.equals", "path": "value", "value": 1}],
    }
    measured = adapter._run_service_case("https://example.invalid", case)
    assert (
        measured["case_id"] == "one"
        and "http_status" not in measured
        and ticks[0] == 0.5
    )
    fresh = adapter._run_service_case("https://example.invalid", case)
    assert fresh["http_status"] == 200


def test_service_cases_share_remaining_budget_then_fresh_run_can_observe(monkeypatch):
    import io

    from cluster_profiles import fleet_qualification

    ticks = [0.0]
    monkeypatch.setattr(fleet_qualification.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(
        fleet_qualification.time,
        "sleep",
        lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
    )
    opened = []
    replies = iter([b"unknown", b'{"value":1}'])

    class Reply(io.BytesIO):
        status = 200

    def open_reply(request, timeout):
        opened.append(timeout)
        return Reply(next(replies))

    adapter = fleet_qualification.ServiceSmokeAdapter(_registry(), opener=open_reply)
    case = {
        "id": "budget",
        "method": "GET",
        "path": "/health",
        "body": None,
        "max_response_bytes": 1024,
        "timeout_seconds": 10,
        "assertions": [{"kind": "path.equals", "path": "value", "value": 1}],
    }
    for _ in range(2):
        with not_adopted():
            adapter._run_service_case("https://example.invalid", case, until=0.25)
    assert ticks[0] == 0.25 and opened == [0.25]
    fresh = adapter._run_service_case("https://example.invalid", case)
    assert fresh["http_status"] == 200 and len(opened) == 2

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from cluster_profiles.fleet_qualification import (
    ArtifactJobSmokeAdapter,
    EvidenceLedger,
)
from cluster_profiles.qualification_fixtures import (
    Fixture,
    FixtureError,
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
        manifest_sha256="b" * 64,
    )


def test_glb_fixture_validation_rejects_header_only_transport_stub() -> None:
    with pytest.raises(FixtureError, match="GLB fixture structure"):
        _validate_magic(b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00", "glb")


def test_registry_requires_provenance_and_rejects_unused_blobs(tmp_path: Path) -> None:
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
        "fixtures": {"unused": fixture},
        "recipes": {},
        "special_fixtures": {},
        "service_case_templates": {},
        "service_recipes": {},
    }
    manifest = tmp_path / "fixtures.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(FixtureError, match="provenance.*required"):
        FixtureRegistry.load(manifest)

    fixture["provenance"] = {
        "origin": "generated",
        "source_url": "urn:vonk:qualification-fixture",
        "source_revision": fixture["sha256"],
        "license_spdx": "CC0-1.0",
        "attribution": "Generated test fixture.",
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FixtureError, match="declares unused fixtures: unused"):
        FixtureRegistry.load(manifest)


def test_assertion_parser_rejects_unknown_fields_and_missing_semantics() -> None:
    with pytest.raises(FixtureError, match="assertion fields"):
        _parse_assertion(
            "vonk-forge/image",
            {"kind": "image-metadata", "width": 64, "height": 64, "typo": 1},
        )

    registry = _registry()
    with pytest.raises(FixtureError, match="lacks semantic coverage"):
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
    with pytest.raises(FixtureError, match="character|semantic"):
        _validate_document_archive(_ocr_zip(characters_delta=1), assertion)
    with pytest.raises(FixtureError, match="unsafe"):
        _safe_zip_entries(_ocr_zip(extra_name="../escaped"))


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
    with pytest.raises(FixtureError, match="acknowledgement"):
        _validate_realtime_transcript(corrupted, assertion)


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
    with pytest.raises(FixtureError, match="does not match"):
        _validate_synchronized_media_receipt(
            corrupted + b"\n", {"result.bin": output}, "portable", assertion
        )


def test_registry_fails_closed_for_missing_changed_and_special_fixtures() -> None:
    registry = _registry()

    recipe, blocker = registry.resolve("vonk-forge/image", "a" * 64, "image-job")
    assert recipe is not None and blocker is None
    assert (
        registry.resolve("vonk-forge/image", "c" * 64, "image-job")[1]["code"]
        == "fixture.recipe_digest_mismatch"
    )
    assert (
        registry.resolve("vonk-forge/missing", "a" * 64, "image-job")[1]["code"]
        == "fixture.missing"
    )


class _DownloadClient:
    def download_file(
        self,
        _path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]:
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

    assert evidence["output_files"][0]["sha256"] == digest
    assert evidence["output_manifest_sha256"] == "c" * 64


def test_output_assertions_reject_mime_mismatch() -> None:
    recipe = _registry().recipes["vonk-forge/image"]
    with pytest.raises(FixtureError, match="media-type"):
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
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status_reads: dict[str, int] = {}
        self.uploaded: list[str] = []
        self.request_ids: list[str] = []
        self.created = 0

    def request(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        extra_headers: object = None,
        query: object = None,
    ) -> dict[str, object]:
        self.calls.append((method, path))
        if path == "/api/v1/artifact-jobs/capabilities":
            return {"schema_version": 1, "transport": {}, "storage": {}}
        if path.endswith("/artifact-jobs"):
            assert extra_headers == {"X-Request-ID": extra_headers["X-Request-ID"]}
            self.request_ids.append(extra_headers["X-Request-ID"])
            self.created += 1
            return {"id": f"job-{self.created}", "state": "draft"}
        if re.fullmatch(r"/api/v1/artifact-jobs/job-\d+", path) and method == "GET":
            job_id = path.rsplit("/", 1)[-1]
            self.status_reads[job_id] = self.status_reads.get(job_id, 0) + 1
            if self.status_reads[job_id] == 1:
                return {"id": job_id, "state": "draft"}
            return {
                "id": job_id,
                "state": "succeeded",
                "contract_sha256": "d" * 64,
                "input_manifest_sha256": "e" * 64,
            }
        if path.endswith("/finalize"):
            return {"id": path.split("/")[-2], "state": "ready"}
        if path.endswith("/submit"):
            return {"id": path.split("/")[-2], "state": "queued"}
        if path.endswith("/result"):
            return {
                "id": path.split("/")[-2],
                "output_manifest_sha256": "f" * 64,
                "output_files": [
                    {
                        "name": "result.png",
                        "media_type": "image/png",
                        "size_bytes": len(PNG),
                        "sha256": hashlib.sha256(PNG).hexdigest(),
                    }
                ],
            }
        raise AssertionError((method, path, payload, query))

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, object]:
        assert source.is_file()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_sha256
        assert source.stat().st_size == expected_size
        assert media_type == "text/plain"
        self.uploaded.append(path)
        return {"id": path.split("/")[-3], "state": "draft"}


def test_artifact_adapter_runs_and_ledgers_durable_controller_lifecycle(
    tmp_path: Path,
) -> None:
    registry = _registry()
    adapter = ArtifactJobSmokeAdapter(registry)
    preview = adapter.preview(
        {"visual_recipe": {"interfaces": [{"adapter": "image-job"}]}},
        recipe_key="vonk-forge/image",
        recipe_content_sha256="a" * 64,
    )
    client = _ArtifactClient()
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")

    result = adapter.run(
        client,
        "run-1",
        preview,
        ledger=ledger,
        plan_digest="f" * 64,
        recipe_key="vonk-forge/image",
        timeout_seconds=300,
        poll_interval_seconds=0.1,
        clock=iter([0, 0, 1]).__next__,
        sleeper=lambda _seconds: None,
    )

    assert result["job_id"] == "job-1"
    assert result["output_manifest_sha256"] == "f" * 64
    assert len(client.uploaded) == 1
    assert {row["event"] for row in ledger.records} >= {
        "artifact-job.created",
        "artifact-job.input-uploaded",
        "artifact-job.finalized",
        "artifact-job.submitted",
        "artifact-job.completed",
    }


def test_artifact_adapter_runs_each_digest_bound_case_with_distinct_evidence(
    tmp_path: Path,
) -> None:
    base_registry = _registry()
    primary = base_registry.recipes["vonk-forge/image"]
    supplemental = replace(primary, case_id="alternate")
    recipe = replace(primary, supplemental_cases=(supplemental,))
    registry = FixtureRegistry(
        base_registry.fixtures,
        {recipe.key: recipe},
        {},
        manifest_sha256="b" * 64,
    )
    adapter = ArtifactJobSmokeAdapter(registry)
    preview = adapter.preview(
        {"visual_recipe": {"interfaces": [{"adapter": "image-job"}]}},
        recipe_key=recipe.key,
        recipe_content_sha256=recipe.content_sha256,
    )
    client = _ArtifactClient()
    ledger = EvidenceLedger(tmp_path / "evidence.jsonl")

    result = adapter.run(
        client,
        "run-1",
        preview,
        ledger=ledger,
        plan_digest="f" * 64,
        recipe_key=recipe.key,
        timeout_seconds=300,
        poll_interval_seconds=0.1,
        clock=iter([0, 0, 1, 0, 0, 1]).__next__,
        sleeper=lambda _seconds: None,
    )

    assert result["case_count"] == 2
    assert [case["case_id"] for case in result["cases"]] == ["default", "alternate"]
    assert [case["job_id"] for case in result["cases"]] == ["job-1", "job-2"]
    assert len(set(client.request_ids)) == 2
    events = {row["event"] for row in ledger.records}
    assert "artifact-job.case.default.completed" in events
    assert "artifact-job.case.alternate.completed" in events

    resumed_client = _ArtifactClient()
    resumed = adapter.run(
        resumed_client,
        "run-1",
        preview,
        ledger=ledger,
        plan_digest="f" * 64,
        recipe_key=recipe.key,
        timeout_seconds=300,
        poll_interval_seconds=0.1,
        clock=iter([0, 0, 1, 0, 0, 1]).__next__,
        sleeper=lambda _seconds: None,
    )
    assert resumed["case_count"] == 2
    assert resumed_client.created == 0
    assert resumed_client.uploaded == []

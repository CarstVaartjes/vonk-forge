from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import wave
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
    _glb_metadata,
    _parse_assertion,
    _parse_recipe_fixture,
    _safe_zip_entries,
    _validate_hunyuan_ocr_zip,
    _validate_magic,
    _validate_moss_transcript,
    _wav_metadata,
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


def test_packaged_generic_fixtures_are_digest_and_format_valid() -> None:
    registry = FixtureRegistry.packaged()

    assert set(registry.fixtures) == {
        "foley-video-1s",
        "generic-image-png",
        "generic-mesh-glb",
        "generic-prompt-text",
        "hunyuanocr-upper-bound-config-v1",
        "ltx25-fp8-model-offload-request",
        "ltx25-fp8-sequential-offload-request",
        "minimax-first-keyframe-request",
        "minimax-first-last-keyframes-request",
        "minimax-keyframe-first-png",
        "minimax-keyframe-last-png",
        "moss-frame-a-png",
        "moss-frame-b-png",
        "moss-session-two-frames-v1",
        "moss-session-v1",
        "service-digit7-png",
        "skintokens-rigged-figure",
        "wan-dancer-controls-v1",
        "wan-dancer-music-1s",
    }
    assert all(
        hashlib.sha256(value.content).hexdigest() == value.sha256
        for value in registry.fixtures.values()
    )
    assert len(registry.recipes) == 44
    assert len(registry.special) == 0
    assert len(registry.service_recipes) == 35
    assert sum(len(recipe.all_cases) for recipe in registry.recipes.values()) == 59
    qwen_cases = registry.recipes[
        "vonk-forge/qwen-image-edit-2511-comfyui-single"
    ].all_cases
    assert [case.case_id for case in qwen_cases] == ["default", "two-references"]
    assert [slot for slot, _fixture in qwen_cases[1].inputs].count("image") == 2
    minimax_cases = registry.recipes[
        "vonk-forge/minimax-h3-fl2va-diffusers-single"
    ].all_cases
    assert [case.case_id for case in minimax_cases] == [
        "default",
        "first-keyframe",
        "first-last-keyframes",
    ]
    ltx_cases = registry.recipes[
        "vonk-forge/ltx-2-5-22b-distilled-bf16-diffusers-single"
    ].all_cases
    assert [case.case_id for case in ltx_cases] == [
        "default",
        "fp8-model-offload",
        "fp8-sequential-offload",
    ]
    assert all(fixture.provenance is not None for fixture in registry.fixtures.values())
    with wave.open(
        io.BytesIO(registry.fixtures["wan-dancer-music-1s"].content)
    ) as music:
        assert music.getnframes() / music.getframerate() == 1
    assert _wav_metadata(registry.fixtures["wan-dancer-music-1s"].content) == {
        "channels": 1,
        "sample_rate": 8000,
        "sample_width_bytes": 1,
        "frame_count": 8000,
        "duration_seconds": 1.0,
    }
    assert registry.fixtures["foley-video-1s"].name == "foley-video-1s.mp4"


def test_mesh_fixtures_are_semantic_provenanced_and_executable() -> None:
    registry = FixtureRegistry.packaged()
    cube = registry.fixtures["generic-mesh-glb"]
    rigged = registry.fixtures["skintokens-rigged-figure"]

    assert len(cube.content) == 784
    assert (
        cube.sha256
        == "626e31b8722f1618bfb4f2ea86905fdd1de15703cb98a8ec946072526602126a"
    )
    assert cube.provenance == {
        "origin": "generated",
        "source_url": "urn:vonk:qualification-fixture",
        "source_revision": cube.sha256,
        "license_spdx": "CC0-1.0",
        "attribution": "Generated by Vonk Forge as an auditable unit-cube qualification mesh.",
    }
    assert _glb_metadata(cube.content)["primitive_count"] == 1

    assert len(rigged.content) == 7864
    assert (
        rigged.sha256
        == "3360574c6fb468ed5577bc489256946c9c734d8b21fe64fec0ea31845b7b26d8"
    )
    assert rigged.provenance is not None
    assert (
        rigged.provenance["source_revision"]
        == "5bad5aaa0bbb5d0f9cdc934e626f27d0df1e79b8"
    )
    assert rigged.provenance["license_spdx"] == "CC-BY-4.0"
    assert _glb_metadata(rigged.content) == {
        "mesh_count": 1,
        "primitive_count": 1,
        "accessor_count": 2,
        "binary_bytes": 6864,
        "material_count": 1,
        "texture_count": 0,
        "image_count": 0,
        "skin_count": 0,
    }
    assert "trimesh 5.0.0" in rigged.provenance["attribution"]

    skin = registry.recipes["vonk-forge/skintokens-pytorch-single"]
    texture = registry.recipes["vonk-forge/step1x-3d-texture-pytorch-single"]
    assert skin.inputs[0][1] is rigged
    assert skin.assertions[-1]["profile"] == "skinned"
    assert [fixture.fixture_id for _, fixture in texture.inputs] == [
        "generic-image-png",
        "generic-mesh-glb",
    ]
    assert texture.assertions[-1]["profile"] == "textured"
    assert skin.preview()["inputs"][0]["provenance"] == rigged.provenance


def test_cube_fixture_generator_is_byte_identical(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "cube.glb"
    subprocess.run(
        [
            sys.executable,
            str(root / "tools/generate-qualification-cube-glb"),
            str(output),
        ],
        check=True,
    )
    registry = FixtureRegistry.packaged()
    assert output.read_bytes() == registry.fixtures["generic-mesh-glb"].content


def test_skintokens_derivation_recipe_pins_upstream_and_transform() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "tools/derive-skintokens-rigged-figure").read_text()
    assert "d6be85417d3e256861ee733eea6916093a7af7c79c16366181fd8abcaeb38cf5" in source
    assert 'trimesh.__version__ != "5.0.0"' in source
    assert 'force="mesh", process=True' in source


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
        "schema_version": 1,
        "fixtures": {"unused": fixture},
        "recipes": {},
        "special_fixtures": {},
        "service_case_templates": {},
        "service_recipes": {},
    }
    manifest = tmp_path / "fixtures.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(FixtureError, match="provenance is required"):
        FixtureRegistry.packaged(manifest)

    fixture["provenance"] = {
        "origin": "generated",
        "source_url": "urn:vonk:qualification-fixture",
        "source_revision": fixture["sha256"],
        "license_spdx": "CC0-1.0",
        "attribution": "Generated test fixture.",
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FixtureError, match="declares unused fixtures: unused"):
        FixtureRegistry.packaged(manifest)


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
        "model": "tencent/HunyuanOCR",
        "model_revision": "47644ecc4fc854efa4f505155158831f36773ee4",
        "runtime_source_revision": "c55965d3da1e6f41987abec8068f2e70851318bc",
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


def test_hunyuan_ocr_zip_is_closed_and_semantic() -> None:
    _validate_hunyuan_ocr_zip(_ocr_zip())
    with pytest.raises(FixtureError, match="character|semantic"):
        _validate_hunyuan_ocr_zip(_ocr_zip(characters_delta=1))
    with pytest.raises(FixtureError, match="unsafe"):
        _safe_zip_entries(_ocr_zip(extra_name="../escaped"))


def test_moss_transcript_requires_authority_ack_and_terminal_record() -> None:
    records = [
        {
            "sequence": 0,
            "elapsed_seconds": 0.0,
            "type": "session-start",
            "model_revision": "06b067617677661194cf837970fe3a10f1a0e56d",
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
    _validate_moss_transcript(content)

    corrupted = content.replace(b'"event_index":0', b'"event_index":1')
    with pytest.raises(FixtureError, match="acknowledgement"):
        _validate_moss_transcript(corrupted)


def test_packaged_recipe_bindings_match_the_campaign_matrix_exactly() -> None:
    registry = FixtureRegistry.packaged()
    matrix = (
        Path(__file__).resolve().parents[2]
        / "docs/runbooks/recipe-qualification-matrix.md"
    ).read_text(encoding="utf-8")
    catalog_keys = {
        f"vonk-forge/{slug}"
        for slug in re.findall(r"^\| `([^`]+)` \|", matrix, flags=re.MULTILINE)
    }
    unsupported_topologies = {
        "vonk-forge/glm-5-2-aqlm-vllm-triple",
        "vonk-forge/glm-5-2-quanttrio-vllm-four",
        "vonk-forge/glm-5-3-flash-nvfp4-vllm-four",
        "vonk-forge/inkling-975b-a41b-nvfp4-sglang-eight",
    }
    fixture_keys = (
        set(registry.recipes) | set(registry.special) | set(registry.service_recipes)
    )

    assert len(catalog_keys) == 83
    assert fixture_keys == catalog_keys - unsupported_topologies


def test_current_vllm028_and_variant_bindings_are_exact() -> None:
    registry = FixtureRegistry.packaged()

    gemma = registry.service_recipes["vonk-forge/gemma-4-26b-a4b-vllm028-single"]
    lfm = registry.service_recipes["vonk-forge/lfm2-5-vl-3b-vllm028-single"]
    glm_nvfp4 = registry.service_recipes["vonk-forge/glm-5-3-flash-nvfp4-vllm-dual"]
    glm_exl3 = registry.service_recipes[
        "vonk-forge/glm-5-3-flash-exl3-dflash2-vllm-dual"
    ]
    assert (gemma.content_sha256, gemma.alias) == (
        "078047fc8139b5ed42da608e6d131df791b45ee74b2eedccea45c50f22d39469",
        "gemma-4-26b-a4b-it-vllm028",
    )
    assert (lfm.content_sha256, lfm.alias) == (
        "b86876422bcbc1f2adc21e3f1ea58c9e2a1849ddc15930ac8d07283c943be9b4",
        "lfm2-5-vl-3b-vllm028",
    )
    assert (glm_nvfp4.content_sha256, glm_nvfp4.alias) == (
        "713ef2a7f87998e2a99447e6dabc3838114c41c2ee2977dc47c79d5bd298b059",
        "glm-5.3-flash",
    )
    assert glm_nvfp4.higher_tiers["stress"] == (
        "32K long-context repetition regression and bounded concurrency canaries",
    )
    assert (glm_exl3.content_sha256, glm_exl3.alias) == (
        "a068d343ca6c2962f1df66146280a245dd136c6c6cd28556e7b5f0da08853a04",
        "glm-5.3-flash-exl3",
    )
    assert glm_exl3.higher_tiers["stress"] == (
        "8K, 16K, 100K, 256K, and 300K cold-prefill ladder plus bounded concurrency canaries",
    )


def test_below_envelope_canary_bindings_are_exact() -> None:
    registry = FixtureRegistry.packaged()
    canary = registry.service_recipes[
        "vonk-forge/deepseek-v4-flash-0731-sparkinfer-target-only-canary-single"
    ]

    assert (
        canary.content_sha256,
        canary.alias,
        tuple(case.case_id for case in canary.cases),
    ) == (
        "a6ec867073cea61fd2e5daff92695c2a79fbb1371f9047ad54833e1398d98dc9",
        "deepseek-v4-flash-0731-spark",
        ("M0", "A391"),
    )
    assert canary.higher_tiers["stress"] == (
        "Physical memory headroom, 252K cold-prefill, and bounded concurrency canaries",
    )

    laguna = registry.service_recipes[
        "vonk-forge/laguna-s-2-1-nvfp4-vllm-low-memory-canary-single"
    ]
    assert (laguna.content_sha256, laguna.alias) == (
        "c384397f261aba1328f6e167041f04975cca67f111bcf89afaae41c1a0ffecf0",
        "laguna-s-2-1-low-memory-canary",
    )

    nemotron = registry.service_recipes[
        "vonk-forge/nemotron-3-5-lightning-dspark-lowmem-canary-single"
    ]
    assert (nemotron.content_sha256, nemotron.alias) == (
        "87c61bb4c3cc3fda60aa510370ebd44af482da235de1b741aba7a0d964cae561",
        "nemotron-3-5-lightning-dspark-low-memory-canary",
    )

    ltx = registry.recipes["vonk-forge/ltx-2-5-22b-distilled-fp8-cast-diffusers-single"]
    assert ltx.content_sha256 == (
        "0b26a789538751b9c7cd899e1ef4d60dec4f1bf215b75cc83b705abf88c77c7c"
    )
    assert tuple(fixture.fixture_id for _, fixture in ltx.inputs) == (
        "generic-prompt-text",
    )
    assert ltx.assertions[-1] == {
        "kind": "semantic-receipt",
        "media_type": "application/json",
        "profile": "fp8-cast-sequential-offload",
    }

    wan = registry.recipes["vonk-forge/wan-dancer-14b-disk-offload-pytorch-single"]
    original_wan = registry.recipes["vonk-forge/wan-dancer-14b-pytorch-single"]
    assert wan.content_sha256 == (
        "84dc412a64fc5d9011ffde30a034077c203da273428fa9fb16ef27421b4c0c5a"
    )
    assert wan.inputs == original_wan.inputs
    assert wan.output_limits == original_wan.output_limits
    assert wan.assertions == original_wan.assertions
    assert tuple(case.case_id for case in wan.all_cases) == (
        "default",
        "optional-controls",
    )

    expected_artifacts = {
        "vonk-forge/flux-2-klein-4b-comfyui-single": "2169cae98b93ef16b8ea63c508b0319a32736fb309191a0e4b0e3c73743898f6",
        "vonk-forge/flux-2-klein-4b-nvfp4-comfyui-single": "d7b92ff1327c7cd0b92970556bd17ea66ec3af95291c1a8890ccb7c200cd5d5e",
        "vonk-forge/ltx-2-19b-dev-bf16-diffusers-single": "0addc2134149c337544fcc6c67d375e24b9a202f4ee88971c24826b3ae893f12",
        "vonk-forge/ltx-2-19b-dev-fp4-pytorch-single": "baff135e07ea79ae4eb19729ee543edd77805a26c9b3922839ab58a63188b2e1",
        "vonk-forge/ltx-2-19b-distilled-diffusers-single": "476a81e835313af326cc29a4f965a3c8d239a48525e1c321a22c990b464b1ae0",
        "vonk-forge/ltx-2-19b-distilled-fp8-diffusers-single": "110bc132308331799923cf3f00f44e49c8cba4ce910031c4cb602b62ffa81984",
        "vonk-forge/ltx-2-3-22b-distilled-1-1-diffusers-single": "c2279d47dec15e776ba9498d896ddb4476d39b17c681576b61c36cf69a617316",
        "vonk-forge/minimax-h3-fl2va-diffusers-single": "3d0cc380ae32b9c19e9b0cf2da5a8dd5522dd63690d166c65f41604ca106f1e6",
        "vonk-forge/qwen-image-2512-comfyui-single": "681b33b3c55a91597692ace91c35b099b9278d4eebef597456f5709e0e228190",
        "vonk-forge/qwen-image-2512-fp8-lightning-comfyui-single": "b7ffa44be4ae99feb10716ee5b46ba68a48632c47072579311f0bcc0b7ee2eab",
        "vonk-forge/qwen-image-edit-2511-comfyui-single": "7c8c441c009ad0d644db586098e08e1aef0da7ac98faaf83564ef30170827136",
        "vonk-forge/qwen-image-edit-2511-fp8mixed-comfyui-single": "8f70cbe740ca7cd2a4a40161395530fe1f9d86b5a1aeab5a57d92423a347b309",
        "vonk-forge/qwen-image-edit-2511-int8-convrot-comfyui-single": "61fda0a7c2ad58e699f5e1bff52dc592f53d518b2cdeea6c74be801e79b30e97",
        "vonk-forge/wan-2-2-i2v-14b-comfyui-single": "373a948d4a6163e29e5b41d5f6971057cb1e4bbb5ed3803e0292a94320d3cbf0",
        "vonk-forge/wan-2-2-t2v-14b-comfyui-single": "63da00c0906859479ecf251b4366219853752228fc8f7bfb12f033e2a6caa021",
        "vonk-forge/wan-2-2-ti2v-5b-comfyui-single": "4c12f075c0afccabbc0fa6b21b0bbabc6fdb14596ec46c4c50358cab32073d5d",
    }
    assert {
        key: registry.recipes[key].content_sha256 for key in expected_artifacts
    } == expected_artifacts


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

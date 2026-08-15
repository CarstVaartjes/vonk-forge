import hashlib
import json
from pathlib import Path

from vonk_control.import_resolution import resolve_import
from vonk_control.model_resolution import ModelFile, SnapshotEnvelope
from vonk_control.registry_resolution import ManifestEnvelope
from vonk_control.workload_run_importer import import_workload_run
from vonk_control.workload_run_source import parse_workload_run_yaml


class Registry:
    def resolve(self, host):
        return ("93.184.216.34",)

    def manifest(self, host, repository, reference, *, maximum_bytes):
        document = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [{"digest": "sha256:" + "b" * 64, "size": 50}],
        }
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return ManifestEnvelope(
            body,
            "sha256:" + hashlib.sha256(body).hexdigest(),
            document["mediaType"],
            "linux/arm64",
            (),
        )


class Models:
    def snapshot(self, repository, revision, *, maximum_files):
        return SnapshotEnvelope(
            repository, revision, (ModelFile("model.safetensors", 100),)
        )


def test_complete_overlays_resolve_import_to_valid_recipe() -> None:
    raw = (
        Path(__file__).parent / "fixtures/workload_run/minimal-vllm.yaml"
    ).read_bytes()
    imported = import_workload_run(parse_workload_run_yaml(raw))
    exact = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    result = resolve_import(
        imported,
        {
            "build_resources": {
                "download_bytes": 50,
                "temporary_bytes": 50,
                "memory_bytes": 100,
                "timeout_seconds": 600,
            },
            "artifact_sizes": {
                "weights": {"download_bytes": 100, "installed_bytes": 150}
            },
            "catalog_references": {
                "model": exact["model"],
                "execution_harness": exact["execution"]["harness"],
                "runtime_distribution": exact["runtime"]["distribution"],
                "patch_bundle": exact["execution"]["patch_bundle"],
            },
            "topology_resources": {
                "entrypoint": {
                    "disk": {
                        "image_bytes": 50,
                        "artifact_bytes": 150,
                        "staging_bytes": 50,
                        "cache_bytes": 10,
                        "rollback_bytes": 50,
                        "safety_margin_bytes": 20,
                    },
                    "memory": {
                        "kind": "unified",
                        "startup_peak_bytes": 225,
                        "steady_state_bytes": 200,
                        "runtime_growth_bytes": 25,
                        "system_reserve_bytes": 25,
                    },
                }
            },
            "security_acknowledged": True,
        },
        registry=Registry(),
        models=Models(),
    )

    assert result.runnable is True
    assert (
        result.bundle.files["Dockerfile"]
        .decode()
        .startswith("FROM ghcr.io/example/vllm@sha256:")
    )
    assert result.document["build"]["context"]["sha256"] == result.bundle.sha256
    assert result.document["artifacts"][0]["download_bytes"] == 100
    assert result.document["topology"]["name"] == "solo"
    assert "deployment" + "_profiles" not in result.document
    assert not result.blockers

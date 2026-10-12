"""Identical content at two paths counts once in every byte total."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from vonk_agent_protocol import DistributionObject
from vonk_control.distribution_executor.identity import DistributionIdentity
from vonk_control.model_cache import ArtifactSetManifest, ArtifactSpec

TOKENIZER = b"{}" * 50
WEIGHTS = b"w" * 300


def _spec(path: str, payload: bytes) -> ArtifactSpec:
    digest = hashlib.sha256(payload).hexdigest()
    return ArtifactSpec(
        key=f"artifact-{path}",
        artifact_id=path,
        path=path,
        kind="huggingface.file",
        repository=None,
        source=f"https://example.invalid/{path}",
        revision=None,
        sha256=digest,
        expected_bytes=len(payload),
        roles=("weights",),
    )


def test_two_paths_with_one_digest_agree_across_totals() -> None:
    specs = (
        _spec("model.bin", WEIGHTS),
        _spec("tokenizer.json", TOKENIZER),
        _spec("dflash/tokenizer.json", TOKENIZER),
    )
    manifest = ArtifactSetManifest(
        model_content_sha256="a" * 64,
        recipe_revision_sha256=None,
        model_content_digests=("a" * 64,),
        artifacts=specs,
    )
    objects = tuple(
        DistributionObject(
            name=item.path, sha256=item.sha256, bytes=item.expected_bytes, kind="model"
        )
        for item in specs
    )
    source = SimpleNamespace(objects_for_set=lambda _digest: objects)
    identity = DistributionIdentity(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(source=source),  # type: ignore[arg-type]
        clock=None,
    )
    plan = SimpleNamespace(
        preparation=SimpleNamespace(
            model=SimpleNamespace(
                artifact_set_sha256=manifest.digest,
                artifact_set_bytes=manifest.expected_bytes,
            )
        ),
        storage=SimpleNamespace(artifact_digests=[item.sha256 for item in specs]),
    )
    placed, _digest, total = identity._model_objects(
        plan,  # type: ignore[arg-type]
        SimpleNamespace(phase_results=[]),  # type: ignore[arg-type]
    )
    assert len(placed) == 3  # every path is still placed
    assert total == manifest.expected_bytes == len(WEIGHTS) + len(TOKENIZER)

"""Identical content at two paths counts once in every byte total."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from vonk_agent_protocol import DistributionAssignment, DistributionObject
from vonk_control.distribution.types import plan_objects
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
    compiled = SimpleNamespace(
        artifacts=[
            SimpleNamespace(
                path=item.path, sha256=item.sha256, size_bytes=item.expected_bytes
            )
            for item in specs
        ]
    )
    sent = plan_objects(compiled)  # type: ignore[arg-type]
    assert [item.name for item in sent] == ["model.bin", "tokenizer.json"]
    assert sum(item.bytes for item in sent) == manifest.expected_bytes


def test_assignment_carries_each_digest_once() -> None:
    """The node stores objects by digest, so duplicate content is sent once."""
    objects = tuple(
        DistributionObject(
            name=path,
            sha256=hashlib.sha256(TOKENIZER).hexdigest(),
            bytes=100,
            kind="model",
        )
        for path in ("tokenizer.json", "dflash/tokenizer.json")
    )
    identity = DistributionIdentity(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 10, 12, tzinfo=UTC),
    )
    plan = SimpleNamespace(plan_digest="b" * 64, mapping=None)
    image = SimpleNamespace(
        address="c" * 64,
        image_digest="sha256:" + "d" * 64,
        config_digest="sha256:" + "e" * 64,
    )
    assignment = identity._assignment(
        plan,  # type: ignore[arg-type]
        "spk_" + "1" * 32,
        objects,
        image,  # type: ignore[arg-type]
        model_set_digest="a" * 64,
    )
    assert [item.name for item in assignment.objects] == ["tokenizer.json"]
    with pytest.raises(ValueError, match="duplicated"):
        DistributionAssignment(
            objects=objects,
            oci_image_digest=image.image_digest,
            oci_image_config_digest=image.config_digest,
        )

"""Identical content at two paths counts once in every byte total."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from vonk_agent_protocol.distribution import DistributionAssignment, DistributionObject
from vonk_control.distribution.types import plan_objects
from vonk_control.distribution_executor.identity import DistributionIdentity
from vonk_control.model_cache.artifacts import ArtifactSetManifest, ArtifactSpec

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


def test_every_node_is_delivered_the_union_of_its_roles_files(monkeypatch) -> None:
    """Role-split plans each name their own files; the model is the unit, so
    every node gets all of them and the bytes equal the prepared set's."""
    from contextlib import nullcontext

    from vonk_control.distribution_executor import identity as identity_module

    def plan(*files: tuple[str, int]) -> SimpleNamespace:
        return SimpleNamespace(
            artifacts=[
                SimpleNamespace(
                    path=name,
                    sha256=hashlib.sha256(name.encode()).hexdigest(),
                    size_bytes=size,
                )
                for name, size in files
            ]
        )

    stored = SimpleNamespace(
        model_artifact_set_sha256="a" * 64,
        compiled_execution_plans={
            "entrypoint-node": plan(("shared", 10), ("rank0", 3)),
            "worker-node": plan(("shared", 10), ("rank1", 5)),
        },
    )
    monkeypatch.setattr(
        identity_module, "parse_stored_installation_plan", lambda _plan: stored
    )
    session = SimpleNamespace(
        get=lambda *_args: SimpleNamespace(plan={}),
    )
    identity = DistributionIdentity(
        lambda: nullcontext(session),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 10, 12, tzinfo=UTC),
    )
    plan_row = SimpleNamespace(
        installation_id="installation-1",
        preparation=SimpleNamespace(
            model=SimpleNamespace(artifact_set_sha256="a" * 64, artifact_set_bytes=18)
        ),
    )

    objects, digest, size = identity._model_objects(
        plan_row,  # type: ignore[arg-type]
        SimpleNamespace(phase_results=[]),  # type: ignore[arg-type]
    )

    assert sorted(item.name for item in objects) == ["rank0", "rank1", "shared"]
    assert (digest, size) == ("a" * 64, 18)

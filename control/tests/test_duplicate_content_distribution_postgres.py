"""A model set with identical content at two paths registers and serves.

The HunyuanOCR DFlash set holds ``tokenizer.json`` and ``dflash/tokenizer.json``
(and two chat templates) with one digest each.  The assignment carries each
digest once; registration and serving decide against the stored manifest by
content, and a real mismatch ends with both sides named instead of an
observe-again retry.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    DistributionCode,
    DistributionObject,
    UnknownOutcomeError,
)
from vonk_control.distribution import (
    CompositeObjectSource,
    DistributionError,
    DistributionService,
    ModelCacheObjectSource,
)
from vonk_control.distribution_executor.identity import DistributionIdentity
from vonk_control.failure_classification import is_transient
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import AgentNode, Base

from .test_nas_cache_acceptance import (
    NODE_A,
    _artifact,
    _download,
    _prebuilt_oci_source,
    _read_object,
)

WEIGHTS = b"dflash target weights"
TOKENIZER = b'{"tokenizer": "shared by target and draft"}'
TEMPLATE = b"{{ shared chat template }}"


def _world(postgres_engine, tmp_path: Path):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_A, state="active"))
    cache = ModelCacheService(
        sessions, tmp_path / "nas-cache", reserve_bytes=0, fixture_sources=True
    )
    model_pin, recipe_pin = "1" * 64, "2" * 64
    files = [
        ("weights", "model.safetensors", WEIGHTS),
        ("tokenizer", "tokenizer.json", TOKENIZER),
        ("draft-tokenizer", "dflash/tokenizer.json", TOKENIZER),
        ("chat-template", "chat_template.jinja", TEMPLATE),
        ("draft-chat-template", "dflash/chat_template.jinja", TEMPLATE),
    ]
    artifacts = [
        _artifact(
            tmp_path,
            artifact_id,
            path,
            payload,
            model_content_sha256=model_pin,
            token="hf_never_forwarded",
        )
        for artifact_id, path, payload in files
    ]
    set_digest = _download(
        cache,
        artifacts,
        model_content_sha256=model_pin,
        recipe_revision_sha256=recipe_pin,
        request_key="00000000-0000-4000-8000-000000000201",
    )
    oci_source, archive, image_digest = _prebuilt_oci_source(
        tmp_path / "prebuilt-oci", b"prebuilt OCI archive"
    )
    model_source = ModelCacheObjectSource.from_service(cache)
    distribution = DistributionService(
        CompositeObjectSource(model_source, oci_source), sessions=sessions
    )
    identity = DistributionIdentity(
        sessions,
        None,  # type: ignore[arg-type]
        distribution,
        clock=lambda: datetime.now(UTC),
    )
    image = SimpleNamespace(
        address=archive, image_digest=image_digest, config_digest="sha256:" + "9" * 64
    )
    return set_digest, model_source, distribution, identity, image


def _assignment(identity, image, set_digest, objects, plan_digest="3" * 64):
    return identity._assignment(
        SimpleNamespace(plan_digest=plan_digest, mapping=None),
        NODE_A,
        objects,
        image,
        model_set_digest=set_digest,
    )


def test_identical_content_at_two_paths_registers_and_serves(
    postgres_engine, tmp_path: Path
) -> None:
    set_digest, model_source, distribution, identity, image = _world(
        postgres_engine, tmp_path
    )
    manifest_objects = model_source.objects_for_set(set_digest)
    shared = hashlib.sha256(TOKENIZER).hexdigest()
    assert {item.name for item in manifest_objects if item.sha256 == shared} == {
        "tokenizer.json",
        "dflash/tokenizer.json",
    }
    assignment = _assignment(identity, image, set_digest, manifest_objects)
    # The wire carries each digest once; the stored manifest keeps every path.
    assert sorted(item.sha256 for item in assignment.objects) == sorted(
        {item.sha256 for item in manifest_objects}
    )

    distribution.register(assignment)

    for payload in (WEIGHTS, TOKENIZER, TEMPLATE):
        digest = hashlib.sha256(payload).hexdigest()
        assert (
            _read_object(
                distribution,
                node_id=NODE_A,
                plan_digest=assignment.plan_digest,
                digest=digest,
            )
            == payload
        )


def test_a_stale_process_copy_of_the_manifest_does_not_decide(
    postgres_engine, tmp_path: Path
) -> None:
    set_digest, model_source, distribution, identity, image = _world(
        postgres_engine, tmp_path
    )
    stored = model_source.objects_for_set(set_digest)
    # This process's copy has lost a file; the stored manifest still has it.
    model_source._manifests[set_digest] = stored[:1]

    distribution.register(_assignment(identity, image, set_digest, stored))

    assert model_source.objects_for_set(set_digest) == stored


def test_a_set_the_stored_manifest_disowns_ends_naming_both_sides(
    postgres_engine, tmp_path: Path
) -> None:
    set_digest, model_source, distribution, identity, image = _world(
        postgres_engine, tmp_path
    )
    stored = model_source.objects_for_set(set_digest)
    foreign = DistributionObject(
        name="foreign.bin", sha256="f" * 64, bytes=7, kind="model"
    )
    weights = hashlib.sha256(WEIGHTS).hexdigest()
    without_weights = (
        *(item for item in stored if item.sha256 != weights),
        foreign,
    )
    assignment = _assignment(identity, image, set_digest, without_weights)

    for _attempt in range(2):
        with pytest.raises(DistributionError) as refused:
            distribution.register(assignment)
        error = refused.value
        # Not an unknown to observe again: the same input fails the same way.
        assert not isinstance(error, UnknownOutcomeError)
        assert not is_transient(error)
        assert error.code == DistributionCode.MODEL_SET_MISMATCH
        assert "foreign.bin@ffffffffffff" in error.detail
        assert "model.safetensors@" in error.detail

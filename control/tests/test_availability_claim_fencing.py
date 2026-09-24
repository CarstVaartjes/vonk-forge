"""Expired executors cannot write through a newer availability claim."""

import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_control.models import Base, Job, RuntimeImageAuthorization, User
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityService,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
    RuntimeImageReceipt,
    RuntimeImageReferenceIntent,
    prepare_runtime_image,
    read_runtime_image_reference_intent,
)

from .test_recipe_image_availability import (
    Transport,
    _add_head,
    _add_revision,
    _recipe,
    _reference_receipt,
    _runtime,
)

_HOLD_LOCK = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(60)\n"
)


def _hold_lock(path: str) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, path], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


@pytest.fixture
def claimed_image(tmp_path, postgres_engine):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    with sessions.begin() as session:
        revision = _add_revision(session, "claim-image", recipe)
        _add_head(session, revision)
    now = datetime.now(UTC)
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    transport = Transport()
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        clock=lambda: now,
    )
    parent = service.start(revision.id, actor="operator", request_id=str(uuid.uuid4()))
    claim = service.claim_pending(limit=1, owner_id="worker")[0]
    return sessions, service, storage, transport, recipe, now, parent, claim


def _take_over(sessions, service, parent, now, *, owner="replacement"):
    with sessions.begin() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "claim_until": (now - timedelta(seconds=1)).isoformat()
        }
    claim = service.claim_pending(limit=1, owner_id=owner)[0]
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        expected = (row.state, row.current_attempt, deepcopy(row.payload), row.result)
    return claim, expected


@pytest.mark.parametrize(
    "boundary", ["progress", "model_progress", "receipt", "success", "model_wait"]
)
def test_expired_callback_preserves_the_new_claim(claimed_image, monkeypatch, boundary):
    sessions, service, storage, transport, recipe, now, parent, original = claimed_image
    takeovers = []

    def takeover():
        assert not takeovers
        takeovers.append(_take_over(sessions, service, parent, now))

    if boundary in {"success", "model_wait"}:
        receipt = prepare_runtime_image(
            recipe,
            runtime=_runtime(),
            storage=storage,
            transport=transport,
            now=now,
        )
        with sessions.begin() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            row.payload = dict(row.payload) | {"image_result": receipt.to_mapping()}

    with monkeypatch.context() as patch:
        if boundary == "progress":
            pull = transport.pull_and_export

            def pull_after_takeover(*args, **kwargs):
                result = pull(*args, **kwargs)
                takeover()
                return result

            patch.setattr(transport, "pull_and_export", pull_after_takeover)
        elif boundary == "model_progress":

            def observe_after_takeover(*_args, **_kwargs):
                takeover()
                return {"id": str(uuid.uuid4()), "state": "running"}

            patch.setattr(service, "_current_model_child", observe_after_takeover)
        elif boundary == "receipt":
            persist = service._persist_receipt

            def persist_after_takeover(*args, **kwargs):
                takeover()
                return persist(*args, **kwargs)

            patch.setattr(service, "_persist_receipt", persist_after_takeover)
        else:
            available = storage.build_archive_available

            def available_after_takeover(*args, **kwargs):
                result = available(*args, **kwargs)
                takeover()
                return result

            patch.setattr(storage, "build_archive_available", available_after_takeover)
            if boundary == "model_wait":
                patch.setattr(
                    service,
                    "_current_model_child",
                    lambda *_args, **_kwargs: {
                        "id": str(uuid.uuid4()),
                        "state": "running",
                    },
                )
        service.run_claim(original)

    assert len(takeovers) == 1
    replacement, expected = takeovers[0]
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert (row.state, row.current_attempt, row.payload, row.result) == expected
        assert session.scalar(select(RuntimeImageAuthorization)) is None
    if boundary in {"progress", "receipt", "model_progress"}:
        # Verified unassociated bytes can be adopted by the current executor;
        # the stale attempt cannot grant current-revision authorization itself.
        transferred_references: list[RuntimeImageReferenceIntent] = []
        if boundary == "receipt":
            with sessions() as session:
                operation = session.get(Job, parent.id)
                assert operation is not None
                prior_reference = read_runtime_image_reference_intent(
                    operation.payload["image_reference_intent"]
                )
            assert prior_reference.attempt < replacement.execution_attempt
            persist_reference = service._persist_provisional_image_reference

            def capture_transferred_reference(
                claim: RecipeImageAvailabilityClaim,
                *,
                receipt: RuntimeImageReceipt,
            ) -> None:
                persist_reference(claim, receipt=receipt)
                with sessions() as session:
                    current = session.get(Job, parent.id)
                    assert current is not None
                    transferred_references.append(
                        read_runtime_image_reference_intent(
                            current.payload["image_reference_intent"]
                        )
                    )

            monkeypatch.setattr(
                service,
                "_persist_provisional_image_reference",
                capture_transferred_reference,
            )
        service.run_claim(replacement)
        assert service.get(parent.id).state == "succeeded"
        if boundary == "receipt":
            assert len(transferred_references) == 1
            transferred_reference = transferred_references[0]
            assert transferred_reference.oci_archive_sha256 == (
                prior_reference.oci_archive_sha256
            )
            assert transferred_reference.attempt == replacement.execution_attempt
            assert transferred_reference.claim_owner == replacement.claim_owner
        with sessions() as session:
            assert session.scalar(select(RuntimeImageAuthorization)) is not None


def test_delayed_claim_cannot_execute_when_the_worker_name_is_reused(claimed_image):
    sessions, service, _storage, transport, _recipe, now, parent, original = (
        claimed_image
    )
    replacement, expected = _take_over(
        sessions, service, parent, now, owner=original.claim_owner
    )
    assert service._renew_claim(original) is False
    service.run_claim(original)
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert (row.state, row.current_attempt, row.payload, row.result) == expected
    assert transport.calls == 0
    service.run_claim(replacement)
    assert service.get(parent.id).state == "succeeded"


@pytest.mark.parametrize("action", ["renew", "execute"])
def test_expired_lease_cannot_be_revived_without_a_new_claim(claimed_image, action):
    sessions, service, _storage, transport, _recipe, now, parent, original = (
        claimed_image
    )
    with sessions.begin() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        row.payload = dict(row.payload) | {
            "claim_until": (now - timedelta(seconds=1)).isoformat()
        }
        expected = deepcopy(row.payload)
    if action == "renew":
        assert service._renew_claim(original) is False
    else:
        service.run_claim(original)
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert row.state == "running"
        assert row.payload == expected
    assert transport.calls == 0
    replacement = service.claim_pending(limit=1, owner_id=original.claim_owner)[0]
    service.run_claim(replacement)
    assert service.get(parent.id).state == "succeeded"


def test_cancelled_image_owner_recovers_after_publication_process_dies(claimed_image):
    sessions, service, storage, _transport, _recipe, _now, parent, claim = claimed_image
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    receipt = _reference_receipt()
    with storage.publication_lock(receipt.oci_archive_sha256):
        service._persist_provisional_image_reference(claim, receipt=receipt)

    lock_path = str(
        storage.root / ".publication-locks" / f"{receipt.oci_archive_sha256}.lock"
    )
    holder = _hold_lock(lock_path)
    try:
        cancelled = service.cancel(
            parent.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            reason="stop after verified publication intent",
        )
        assert cancelled.state == "cancelling"
        assert service.reconcile_cancellations() == 0
        with sessions() as session:
            row = session.get(Job, parent.id)
            assert row is not None
            assert row.state == "cancelling"
            assert row.payload["claim_owner"] == claim.claim_owner
            assert "image_reference_intent" in row.payload
            lease = datetime.fromisoformat(row.payload["claim_until"])
            assert lease > datetime.now(UTC)
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    # Process death releases the kernel fence even with an unexpired SQL lease.
    assert service.reconcile_cancellations() == 1
    assert service.get(parent.id).state == "cancelled"
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert row.state == "cancelled"
        assert row.payload.get("claim_owner") is None
        assert "image_reference_intent" not in row.payload
        assert session.scalar(select(RuntimeImageAuthorization)) is None

    with pytest.raises(RuntimeImagePreparationError) as stale:
        service._persist_provisional_image_reference(claim, receipt=receipt)
    assert stale.value.code == "recipe_image.claim_lost"


def test_contended_progress_releases_the_artifact_worker(claimed_image, monkeypatch):
    sessions, service, _storage, transport, _recipe, _now, parent, claim = claimed_image
    pull = transport.pull_and_export
    blockers = []

    def pull_while_owner_locked(*args, **kwargs):
        evidence = pull(*args, **kwargs)
        blocker = sessions()
        blockers.append(blocker)
        blocker.begin()
        assert blocker.get(Job, parent.id, with_for_update=True) is not None
        kwargs["progress"]("download", 1, 1)
        return evidence

    with monkeypatch.context() as patch:
        patch.setattr(transport, "pull_and_export", pull_while_owner_locked)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.run_claim, claim)
            try:
                future.result(timeout=5)
                assert len(blockers) == 1
            finally:
                for blocker in blockers:
                    blocker.rollback()
                    blocker.close()
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        assert row.state == "running"
        assert session.scalar(select(RuntimeImageAuthorization)) is None
    service.run_claim(claim)
    assert service.get(parent.id).state == "succeeded"

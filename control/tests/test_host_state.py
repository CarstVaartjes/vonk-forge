from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from vonk_control import host_state as host_state_module
from vonk_control.host_state import (
    HostGenerationStore,
    HostOperationLock,
    HostOperationPlan,
    HostStateConflict,
    PhaseJournal,
    SelectionReceipt,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _plan(
    *,
    operation_id: str = "operation-1",
    target_sha256: str = SHA_B,
) -> HostOperationPlan:
    return HostOperationPlan(
        operation_id=operation_id,
        plan_digest=f"sha256:{SHA_A}",
        generation_id="gen-" + target_sha256[:24],
        platform_target_name=f"platform/releases/1.2.0/{target_sha256}.json",
        platform_target_sha256=target_sha256,
        tuf_targets_version=7,
        release_digest=f"sha256:{target_sha256}",
        build_digest=f"sha256:{SHA_C}",
        platform_version="1.2.0",
        deployment_bundle_digest=f"sha256:{SHA_D}",
        api_image=f"ghcr.io/example/api@sha256:{SHA_A}",
        worker_image=f"ghcr.io/example/worker@sha256:{SHA_B}",
        database_revision="0001_fleet_library_baseline",
    )


def _receipt(
    plan: HostOperationPlan, *, previous: str | None = None
) -> SelectionReceipt:
    return SelectionReceipt.from_plan(plan, previous_generation=previous)


def _store(tmp_path: Path) -> HostGenerationStore:
    return HostGenerationStore(
        tmp_path / "control-host",
        tmp_path / "control-identity",
        owner_uid=os.geteuid(),
    )


def _stage_and_commit(store: HostGenerationStore, receipt: SelectionReceipt) -> Path:
    def populate(destination: Path) -> None:
        destination.mkdir(mode=0o700)
        (destination / "compose.yaml").write_text("services: {}\n")

    staged = store.prepare_staging(
        receipt.generation_id,
        populate,
    )
    return store.commit_generation(staged, receipt.generation)


def test_host_state_layout_requires_exact_owner_modes_and_no_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "control-host"
    host.mkdir(mode=0o755)
    with pytest.raises(HostStateConflict, match="mode"):
        HostGenerationStore(
            host, tmp_path / "identity", owner_uid=os.geteuid()
        ).initialize()

    host.chmod(0o700)
    effective_uid = os.geteuid()
    monkeypatch.setattr(host_state_module.os, "geteuid", lambda: effective_uid + 1)
    with pytest.raises(HostStateConflict, match="owner"):
        HostGenerationStore(
            host, tmp_path / "identity", owner_uid=effective_uid + 1
        ).initialize()

    linked = tmp_path / "linked-host"
    linked.symlink_to(host, target_is_directory=True)
    with pytest.raises(HostStateConflict, match="unsafe"):
        HostGenerationStore(
            linked, tmp_path / "identity-2", owner_uid=effective_uid
        ).initialize()


def test_operation_lock_is_exclusive_and_rejects_unsafe_lock_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "control-host"
    first = HostOperationLock(root, owner_uid=os.geteuid())
    second = HostOperationLock(root, owner_uid=os.geteuid())
    with first, pytest.raises(HostStateConflict, match="operation is active"), second:
        pass

    lock_path = root / "operation.lock"
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"")
    lock_path.unlink()
    lock_path.symlink_to(outside)
    with pytest.raises(HostStateConflict, match="unsafe"):
        HostOperationLock(root, owner_uid=os.geteuid()).__enter__()

    lock_path.unlink()
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    (tmp_path / "lock-hardlink").hardlink_to(lock_path)
    with pytest.raises(HostStateConflict, match="hard-link"):
        HostOperationLock(root, owner_uid=os.geteuid()).__enter__()


def test_nested_same_operation_lock_does_not_release_outer_lock(tmp_path: Path) -> None:
    root = tmp_path / "control-host"
    lock = HostOperationLock(root, owner_uid=os.geteuid())
    with lock:
        with lock:
            pass
        with pytest.raises(
            HostStateConflict, match="operation is active"
        ), HostOperationLock(root, owner_uid=os.geteuid()):
            pass


def test_candidate_and_active_projections_are_distinct_and_canonical(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    plan = _plan()
    candidate = store.project_candidate(plan)

    assert candidate.projection_kind == "candidate"
    assert store.load_candidate(plan.operation_id) == candidate
    assert store.load_active() is None
    candidate_raw = (
        tmp_path / "control-identity/candidates/operation-1.json"
    ).read_bytes()
    assert (
        candidate_raw
        == (
            json.dumps(json.loads(candidate_raw), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
    )
    assert (
        tmp_path / "control-identity/candidates/operation-1.json"
    ).stat().st_mode & 0o777 == 0o444

    receipt = _receipt(plan)
    _stage_and_commit(store, receipt)
    selected = store.select(receipt)
    assert selected.projection_kind == "active"
    assert selected.generation_id == plan.generation_id
    assert selected.projection_sequence == 1
    assert store.load_active() == selected

    candidate_path = tmp_path / "control-identity/candidates/operation-1.json"
    active_path = tmp_path / "control-identity/active.json"
    active_bytes = active_path.read_bytes()
    candidate_path.chmod(0o644)
    candidate_path.write_bytes(active_bytes)
    candidate_path.chmod(0o444)
    with pytest.raises(HostStateConflict, match="candidate projection kind"):
        store.load_candidate(plan.operation_id)

    active_path.chmod(0o644)
    active_path.write_bytes(candidate_raw)
    active_path.chmod(0o444)
    with pytest.raises(HostStateConflict, match="active projection kind"):
        store.load_active()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("platform_target_name", "platform-release.json"),
        (
            "platform_target_name",
            f"platform/releases/1.3.0/{SHA_B}.json",
        ),
        (
            "platform_target_name",
            f"platform/releases/1.2.0/{SHA_C}.json",
        ),
        ("release_digest", f"sha256:{SHA_C}"),
    ),
)
def test_operation_identity_requires_exact_versioned_target_binding(
    field: str,
    value: object,
) -> None:
    fields = dict(_plan().__dict__)
    fields[field] = value
    with pytest.raises(ValueError, match="identity"):
        HostOperationPlan(**fields)


def test_active_projection_recomputes_immutable_receipt_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_plan())
    _stage_and_commit(store, receipt)
    store.select(receipt)
    active = tmp_path / "control-identity/active.json"
    document = json.loads(active.read_bytes())
    document["generation_receipt_sha256"] = SHA_A
    active.chmod(0o644)
    active.write_bytes(
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    active.chmod(0o444)

    with pytest.raises(HostStateConflict, match="receipt digest binding"):
        store.load_active()


def test_projection_read_uses_safe_dirfd_and_rejects_links_hardlinks_and_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    plan = _plan()
    receipt = _receipt(plan)
    _stage_and_commit(store, receipt)
    store.select(receipt)
    active = tmp_path / "control-identity/active.json"

    outside = tmp_path / "outside-active"
    outside.write_bytes(active.read_bytes())
    active.unlink()
    active.symlink_to(outside)
    with pytest.raises(HostStateConflict, match="unsafe"):
        store.load_active()

    active.unlink()
    active.write_bytes(outside.read_bytes())
    active.chmod(0o444)
    (tmp_path / "active-hardlink").hardlink_to(active)
    with pytest.raises(HostStateConflict, match="hard-link"):
        store.load_active()
    (tmp_path / "active-hardlink").unlink()

    original_read = host_state_module.os.read
    raced = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal raced
        chunk = original_read(descriptor, count)
        if not raced and chunk.startswith(b"{"):
            raced = True
            active.chmod(0o644)
            active.write_bytes(active.read_bytes() + b" ")
            active.chmod(0o444)
        return chunk

    monkeypatch.setattr(host_state_module.os, "read", racing_read)
    with pytest.raises(HostStateConflict, match="changed while being read"):
        store.load_active()


def test_atomic_projection_replacement_is_visible_by_directory_lookup(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_plan = _plan(target_sha256=SHA_B)
    first_receipt = _receipt(first_plan)
    _stage_and_commit(store, first_receipt)
    first = store.select(first_receipt)

    identity_fd = os.open(
        tmp_path / "control-identity", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        second_plan = _plan(operation_id="operation-2", target_sha256=SHA_C)
        second_receipt = _receipt(second_plan, previous=first_plan.generation_id)
        _stage_and_commit(store, second_receipt)
        second = store.select(second_receipt)
        descriptor = os.open(
            "active.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=identity_fd
        )
        try:
            observed = json.loads(os.read(descriptor, 64 * 1024))
        finally:
            os.close(descriptor)
    finally:
        os.close(identity_fd)

    assert first.projection_sequence == 1
    assert second.projection_sequence == 2
    assert observed["selection"]["generation"]["generation_id"] == (
        second_plan.generation_id
    )
    assert observed["projection_sequence"] == 2
    assert (tmp_path / "control-host/active-generation").read_text() == (
        second_plan.generation_id + "\n"
    )


def test_generation_receipt_is_immutable_and_bound_to_active_projection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_plan())
    generation = _stage_and_commit(store, receipt)
    raw = (generation / "generation.json").read_bytes()
    assert (generation / "generation.json").stat().st_mode & 0o777 == 0o400

    active = store.select(receipt)
    assert active.generation_receipt_sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(HostStateConflict, match="already exists"):
        _stage_and_commit(store, receipt)

    (tmp_path / "receipt-hardlink").hardlink_to(generation / "generation.json")
    with pytest.raises(HostStateConflict, match="hard-link"):
        store.select(receipt)


def test_existing_generation_can_be_reselected_by_new_rollback_operation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    old_plan = _plan(operation_id="install-old", target_sha256=SHA_B)
    old_selection = _receipt(old_plan)
    old_generation_path = _stage_and_commit(store, old_selection)
    immutable_raw = (old_generation_path / "generation.json").read_bytes()
    store.select(old_selection)

    new_plan = _plan(operation_id="install-new", target_sha256=SHA_C)
    new_selection = _receipt(new_plan, previous=old_plan.generation_id)
    _stage_and_commit(store, new_selection)
    store.select(new_selection)

    rollback = SelectionReceipt.for_generation(
        old_selection.generation,
        operation_id="rollback-new-to-old",
        plan_digest=f"sha256:{SHA_D}",
        previous_generation=new_plan.generation_id,
    )
    selected = store.select(rollback)

    assert selected.generation_id == old_plan.generation_id
    assert selected.operation_id == "rollback-new-to-old"
    assert selected.previous_generation == new_plan.generation_id
    assert (old_generation_path / "generation.json").read_bytes() == immutable_raw


def test_selection_intent_reconciles_projection_first_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_plan())
    _stage_and_commit(store, receipt)
    original = host_state_module._write_atomic_at

    def crash_before_pointer(parent, name, content, **kwargs):
        if name == "active-generation":
            raise OSError("injected pointer crash")
        return original(parent, name, content, **kwargs)

    monkeypatch.setattr(host_state_module, "_write_atomic_at", crash_before_pointer)
    with pytest.raises(OSError, match="pointer crash"):
        store.select(receipt)
    monkeypatch.setattr(host_state_module, "_write_atomic_at", original)

    assert (tmp_path / "control-host/selection.pending.json").is_file()
    selected = store.load_active()
    assert selected is not None and selected.generation_id == receipt.generation_id
    assert store.load_pointer() == receipt.generation_id
    assert not (tmp_path / "control-host/selection.pending.json").exists()


def test_pointer_projection_disagreement_without_intent_fails_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_plan())
    _stage_and_commit(store, receipt)
    store.select(receipt)
    pointer = tmp_path / "control-host/active-generation"
    pointer.write_text("different-generation\n")
    pointer.chmod(0o600)

    with pytest.raises(HostStateConflict, match="pointer and projection disagree"):
        store.load_active()


def test_active_projection_loader_does_not_require_host_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_plan())
    _stage_and_commit(store, receipt)
    expected = store.select(receipt)
    pointer = tmp_path / "control-host/active-generation"
    pointer.write_text("different-generation\n")
    pointer.chmod(0o600)

    assert store.load_active_projection() == expected
    with pytest.raises(HostStateConflict, match="pointer and projection disagree"):
        store.load_active()


def test_interrupted_extraction_staging_is_safely_replaced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    interrupted = tmp_path / "control-host/generations/.gen-a.staging"
    interrupted.mkdir(mode=0o700)
    (interrupted / "partial-file").write_bytes(b"partial")
    (interrupted / "partial-file").chmod(0o644)

    def populate(destination: Path) -> None:
        destination.mkdir(mode=0o700)
        (destination / "complete-file").write_bytes(b"complete")

    staged = store.prepare_staging("gen-a", populate)

    assert staged == interrupted
    assert not (staged / "partial-file").exists()
    assert (staged / "complete-file").read_bytes() == b"complete"


def test_interrupted_staging_recovery_unlinks_planted_links_without_following(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.initialize()
    interrupted = tmp_path / "control-host/generations/.gen-a.staging"
    interrupted.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("preserve me")
    (interrupted / "link").symlink_to(outside)

    def populate(destination: Path) -> None:
        destination.mkdir(mode=0o700)
        (destination / "safe").write_text("safe")

    store.prepare_staging("gen-a", populate)
    assert outside.read_text() == "preserve me"
    assert not (interrupted / "link").exists()


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "mode"))
def test_commit_rejects_unsafe_generation_assets(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    store = _store(tmp_path)
    receipt = _receipt(_plan())
    outside = tmp_path / "outside"
    outside.write_text("outside")

    def populate(destination: Path) -> None:
        destination.mkdir(mode=0o700)
        asset = destination / "asset"
        if unsafe_kind == "symlink":
            asset.symlink_to(outside)
        else:
            asset.write_text("asset")
            if unsafe_kind == "hardlink":
                (tmp_path / "asset-hardlink").hardlink_to(asset)
            else:
                asset.chmod(0o666)

    staged = store.prepare_staging(receipt.generation_id, populate)
    with pytest.raises(HostStateConflict, match="unsafe"):
        store.commit_generation(staged, receipt.generation)


def test_journal_entries_are_contiguous_and_hash_chained(tmp_path: Path) -> None:
    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        clock=lambda: "2026-08-06T10:00:00Z",
        owner_uid=os.geteuid(),
    )
    created = journal.create(plan)
    first = journal.append("authorized", {"target_sha256": SHA_B})
    second = journal.append("bundle-verified", {"bundle_sha256": SHA_D})

    assert created.entries == ()
    assert [entry.sequence for entry in second.entries] == [1, 2]
    assert second.entries[0].previous_entry_digest is None
    assert second.entries[1].previous_entry_digest == second.entries[0].entry_digest
    operation = tmp_path / "control-host/operations/operation-1"
    assert sorted(path.name for path in operation.glob("*.json")) == [
        "0001-authorized.json",
        "0002-bundle-verified.json",
        "plan.json",
    ]
    assert first.entries[0] == second.entries[0]


def test_journal_rejects_gaps_tampering_hardlinks_and_second_pending(
    tmp_path: Path,
) -> None:
    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        owner_uid=os.geteuid(),
    )
    journal.create(plan)
    state = journal.append("authorized", {"target_sha256": SHA_B})
    entry = tmp_path / "control-host/operations/operation-1/0001-authorized.json"
    document = json.loads(entry.read_bytes())
    document["evidence"]["target_sha256"] = SHA_C
    entry.chmod(0o600)
    entry.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    entry.chmod(0o400)
    with pytest.raises(HostStateConflict, match="entry digest"):
        journal.append("bundle-verified", {"bundle_sha256": SHA_D})

    entry.chmod(0o600)
    entry.write_bytes(state.entries[0].raw)
    entry.chmod(0o400)
    (tmp_path / "journal-hardlink").hardlink_to(entry)
    with pytest.raises(HostStateConflict, match="hard-link"):
        journal.append("bundle-verified", {"bundle_sha256": SHA_D})
    (tmp_path / "journal-hardlink").unlink()

    gap = entry.with_name("0003-gap.json")
    gap.write_bytes(state.entries[0].raw)
    gap.chmod(0o400)
    with pytest.raises(HostStateConflict, match="contiguous"):
        journal.append("bundle-verified", {"bundle_sha256": SHA_D})
    gap.unlink()

    other = _plan(operation_id="operation-2", target_sha256=SHA_C)
    with pytest.raises(HostStateConflict, match="another host operation is pending"):
        PhaseJournal(
            tmp_path / "control-host",
            operation_id=other.operation_id,
            owner_uid=os.geteuid(),
        ).create(other)


def test_journal_persists_full_plan_and_bounds_evidence_before_publication(
    tmp_path: Path,
) -> None:
    plan = {
        **_plan().document(),
        "exact_site_config_digest": f"sha256:{SHA_D}",
        "oci_manifest_size": 4096,
    }
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan["operation_id"],
        owner_uid=os.geteuid(),
    )
    state = journal.create(plan)
    assert state.plan_document == plan

    with pytest.raises(HostStateConflict, match="size bound"):
        journal.append("oversized", {"blob": "x" * (64 * 1024)})
    operation = tmp_path / "control-host/operations/operation-1"
    assert not list(operation.glob("0001-*.json"))


def test_journal_plan_and_entry_are_not_visible_until_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        owner_uid=os.geteuid(),
    )
    original_write = host_state_module.os.write

    def fail_write(_descriptor: int, _content: bytes) -> int:
        raise OSError("injected short write")

    monkeypatch.setattr(host_state_module.os, "write", fail_write)
    with pytest.raises(OSError, match="short write"):
        journal.create(plan)
    assert not (tmp_path / "control-host/operations/operation-1").exists()

    monkeypatch.setattr(host_state_module.os, "write", original_write)
    journal.create(plan)
    monkeypatch.setattr(host_state_module.os, "write", fail_write)
    with pytest.raises(OSError, match="short write"):
        journal.append("authorized", {"target_sha256": SHA_B})
    assert not list(
        (tmp_path / "control-host/operations/operation-1").glob("0001-authorized.json")
    )

    monkeypatch.setattr(host_state_module.os, "write", original_write)
    assert (
        journal.append("authorized", {"target_sha256": SHA_B}).entries[-1].phase
        == "authorized"
    )


def test_completed_journal_is_not_returned_as_pending(tmp_path: Path) -> None:
    plan = _plan()
    journal = PhaseJournal(
        tmp_path / "control-host",
        operation_id=plan.operation_id,
        owner_uid=os.geteuid(),
    )
    journal.create(plan)
    journal.append("completed", {"generation_id": plan.generation_id})
    assert (
        PhaseJournal(tmp_path / "control-host", owner_uid=os.geteuid()).load_pending()
        is None
    )

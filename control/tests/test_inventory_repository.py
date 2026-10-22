from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentNode, Base, NodeInventorySnapshot
from vonk_control.stored_json import write_guard_mode

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture


def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inventory.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id="spk_" + "1" * 32,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    "spk_" + "1" * 32,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
            )
        )
    return InventoryRepository(sessions), now


def test_new_snapshot_supersedes_old_but_preserves_evidence(tmp_path) -> None:
    repo, now = repository(tmp_path)
    node = "spk_" + "1" * 32
    first = repo.record(
        InventorySnapshotInput(
            node,
            now,
            1000,
            700,
            500,
            300,
            400,
            250,
            1,
            False,
            ("runtime.vllm.v1",),
            memory_pool="separate",
        )
    )
    second = repo.record(
        InventorySnapshotInput(
            node,
            now + timedelta(seconds=5),
            1000,
            600,
            500,
            250,
            400,
            200,
            1,
            False,
            ("runtime.vllm.v1",),
            memory_pool="separate",
        )
    )
    latest = repo.latest(node, now=now + timedelta(seconds=6), maximum_age=10)
    assert latest.id == second.id and latest.id != first.id
    assert repo.snapshot_count(node) == 2


def test_stale_inventory_is_explicit(tmp_path) -> None:
    repo, now = repository(tmp_path)
    node = "spk_" + "1" * 32
    repo.record(
        InventorySnapshotInput(
            node,
            now,
            1000,
            700,
            500,
            300,
            400,
            250,
            1,
            False,
            ("runtime.vllm.v1",),
            memory_pool="separate",
        )
    )
    assert (
        repo.latest(node, now=now + timedelta(minutes=6), maximum_age=300).stale is True
    )


def test_latest_rejects_malformed_persisted_capabilities(tmp_path) -> None:
    repo, now = repository(tmp_path)
    node = "spk_" + "1" * 32
    stored = repo.record(
        InventorySnapshotInput(
            node,
            now,
            1000,
            700,
            500,
            300,
            400,
            250,
            1,
            False,
            ("runtime.vllm.v1",),
            memory_pool="separate",
        )
    )
    with write_guard_mode(strict=False), repo._sessions.begin() as session:
        snapshot = session.get(NodeInventorySnapshot, stored.id)
        assert snapshot is not None
        # Persist a JSON object where the array contract requires a list, so
        # the read path has to reject malformed stored state.
        object.__setattr__(snapshot, "capabilities", {"bad": "value"})

    with pytest.raises(Exception):  # noqa: B017 -- unreadable evidence must not be returned as valid capacity
        repo.latest(node, now=now, maximum_age=60)
    with repo._sessions.begin() as session:
        snapshot = session.get(NodeInventorySnapshot, stored.id)
        assert snapshot is not None
        snapshot.capabilities = ["runtime.vllm.v1"]
    repaired = repo.latest(node, now=now, maximum_age=60)
    assert repaired is not None and repaired.id == stored.id
    assert repaired.capabilities == ("runtime.vllm.v1",)
    assert repaired.host_memory_free_bytes == 300


@pytest.mark.parametrize(
    "reported", [None, [], ["00000000-0000-4000-8000-000000000001"]]
)
def test_original_directory_items_and_sizes_survive_store_load(
    tmp_path, reported
) -> None:
    """Catches retaining only reconciliation effects and discarding original facts."""
    from vonk_agent_protocol.inventory import InstallationBytes
    from vonk_control.inventory_repository import _validated_inventory

    repo, now = repository(tmp_path)
    node_id = "spk_" + "1" * 32
    value = InventorySnapshotInput(
        node_id,
        now,
        1000,
        700,
        500,
        300,
        400,
        250,
        1,
        False,
        ("runtime.vllm.v1",),
        memory_pool="separate",
    )
    measured = (
        None
        if reported is None
        else [InstallationBytes(installation_id=key, bytes=0) for key in reported]
    )
    report = _validated_inventory(value, observed_at=now).model_copy(
        update={
            "installations": reported,
            "runs": reported,
            "installation_bytes": measured,
        }
    )
    stored = repo.record(value, report=report)
    with repo._sessions() as session:
        loaded = session.get(NodeInventorySnapshot, stored.id)
        assert loaded is not None
        assert loaded.installations == reported
        assert loaded.runs == reported
        assert loaded.installation_bytes == measured
    latest = repo.latest(node_id, now=now, maximum_age=300)
    assert latest.installations == reported
    assert latest.runs == reported
    assert latest.installation_bytes == measured

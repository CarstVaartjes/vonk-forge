from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentNode, Base


def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'inventory.sqlite'}"); Base.metadata.create_all(engine); sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id="spk_"+"1"*32, state="active", architecture="linux-arm64", capabilities=[]))
    return InventoryRepository(sessions), now


def test_new_snapshot_supersedes_old_but_preserves_evidence(tmp_path) -> None:
    repo, now = repository(tmp_path); node = "spk_"+"1"*32
    first = repo.record(InventorySnapshotInput(node, now, 1000, 700, 500, 300, 400, 250, 1, False, ("runtime.vllm.v1",)))
    second = repo.record(InventorySnapshotInput(node, now + timedelta(seconds=5), 1000, 600, 500, 250, 400, 200, 1, False, ("runtime.vllm.v1",)))
    latest = repo.latest(node, now=now + timedelta(seconds=6), maximum_age=10)
    assert latest.id == second.id and latest.id != first.id
    assert repo.snapshot_count(node) == 2


def test_stale_inventory_is_explicit(tmp_path) -> None:
    repo, now = repository(tmp_path); node = "spk_"+"1"*32
    repo.record(InventorySnapshotInput(node, now, 1000, 700, 500, 300, 400, 250, 1, False, ("runtime.vllm.v1",)))
    assert repo.latest(node, now=now + timedelta(minutes=6), maximum_age=300).stale is True

"""Fake Spark agents: they report inventory and answer the orders they are given.

Each Spark answers through the Controller's own result consumer
(``record_node_result``), the boundary the agent API reports into. A Spark keeps
its own truth of which runs it is serving and reports their observations, so a
Spark that is down stops reporting them. Faults are per Spark: down (no
inventory, no answers), late (answers held for some rounds), duplicate (every
answer delivered twice), failing (its next answers are retryable failures) and
short of disk (inventory reports almost no free space).

A result that is lost in transit is the same, to the Controller, as a late one:
the Spark reports it again when it next reaches the Controller. The agent's
claim and lease path is not modelled here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import select
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentOperation, AgentPresence, Job, RecipeRun, RunNode

from ..test_lifecycle_preflight import _finish as complete_preflight
from ..test_recipe_operations import start_evidence
from .world import World

ANSWERED_KINDS = (
    "recipe.install",
    "recipe.start",
    "recipe.stop",
    "recipe.uninstall",
    "runtime.preflight.v1",
)
_ENDED = ("succeeded", "failed", "cancelled", "superseded")
#: What a failing Spark reports: a dependency that was briefly unavailable.
RETRYABLE_FAILURE = {
    "error_code": "runtime_observation_unavailable",
    "failure_kind": "temporary-dependency",
}


#: Free disk a Spark under pressure reports: below the collector's low mark.
_PRESSURED_FREE_FRACTION = 0.05


@dataclass
class Spark:
    node_id: str
    down_steps: int = 0
    late_rounds: int = 0
    duplicate_steps: int = 0
    pressure_steps: int = 0
    fail_next: bool = False
    serving: set[str] = field(default_factory=set)

    @property
    def down(self) -> bool:
        return self.down_steps > 0

    def end_step(self) -> None:
        self.down_steps = max(0, self.down_steps - 1)
        self.duplicate_steps = max(0, self.duplicate_steps - 1)
        self.pressure_steps = max(0, self.pressure_steps - 1)

    def clear(self) -> None:
        self.down_steps = self.late_rounds = 0
        self.duplicate_steps = self.pressure_steps = 0
        self.fail_next = False


@dataclass(frozen=True)
class Order:
    job_id: str
    kind: str
    node_id: str
    operation_id: str
    payload: dict[str, object]


class Sparks:
    def __init__(self, world: World) -> None:
        self.world = world
        self.by_node = {node: Spark(node) for node in world.nodes}
        self.refused: list[str] = []
        inventory = InventoryRepository(world.sessions, clock=world.clock)
        #: What each Spark reports free when it is not short of disk.
        self.disk_free = {
            node: inventory.latest(
                node, now=world.clock(), maximum_age=10**9
            ).disk_free_bytes
            for node in world.nodes
        }

    def spark(self, index: int) -> Spark:
        return self.by_node[self.world.nodes[index]]

    def healthy(self) -> bool:
        """Every Spark is up, answering promptly and has room."""

        return all(
            not spark.down and spark.late_rounds == 0 and spark.pressure_steps == 0
            for spark in self.by_node.values()
        )

    def end_step(self) -> None:
        for spark in self.by_node.values():
            spark.end_step()

    def clear_faults(self) -> None:
        for spark in self.by_node.values():
            spark.clear()

    def round(self) -> None:
        """One agent poll on every Spark: inventory, observations, answers."""

        for spark in self.by_node.values():
            if spark.down:
                continue
            self._report_inventory(spark)
            if spark.late_rounds > 0:
                spark.late_rounds -= 1
                continue
            self._answer(spark)
        self._report_observations()

    def _report_inventory(self, spark: Spark) -> None:
        now = self.world.clock()
        inventory = InventoryRepository(self.world.sessions, clock=self.world.clock)
        latest = inventory.latest(spark.node_id, now=now, maximum_age=10**9)
        if latest.observed_at >= now:
            return  # already reported at this instant
        inventory.record(
            InventorySnapshotInput(
                spark.node_id,
                now,
                latest.disk_total_bytes,
                int(latest.disk_total_bytes * _PRESSURED_FREE_FRACTION)
                if spark.pressure_steps
                else self.disk_free[spark.node_id],
                latest.host_memory_total_bytes,
                latest.host_memory_free_bytes,
                latest.gpu_memory_total_bytes,
                latest.gpu_memory_free_bytes,
                latest.gpu_count,
                latest.artifact_store_read_only,
                latest.capabilities,
                memory_pool=latest.memory_pool,
                fabric_address=latest.fabric_address,
                fabric_bandwidth_mbps=latest.fabric_bandwidth_mbps,
            )
        )
        with self.world.sessions.begin() as session:
            presence = session.get(AgentPresence, spark.node_id)
            if presence is not None:
                presence.observed_at = now

    def _orders(self, node_id: str) -> tuple[Order, ...]:
        with self.world.sessions() as session:
            rows = session.execute(
                select(Job.id, Job.kind, AgentOperation.id, AgentOperation.payload)
                .join(AgentOperation, AgentOperation.parent_job_id == Job.id)
                .where(
                    Job.state == "running",
                    Job.kind.in_(ANSWERED_KINDS),
                    AgentOperation.node_id == node_id,
                    AgentOperation.state.not_in(_ENDED),
                )
                .order_by(Job.created_at, Job.id, AgentOperation.id)
            ).all()
        return tuple(
            Order(job_id, kind, node_id, operation_id, dict(payload or {}))
            for job_id, kind, operation_id, payload in rows
        )

    def _answer(self, spark: Spark) -> None:
        lifecycle = self.world.controller.lifecycle
        orders = self._orders(spark.node_id)
        # Every answer of the round fails, so which order sorts first (product
        # ids are random) cannot change the outcome.
        failed = spark.fail_next and bool(orders)
        spark.fail_next = spark.fail_next and not orders
        for order in orders:
            if order.kind == "runtime.preflight.v1":
                complete_preflight(
                    self.world.sessions,
                    SimpleNamespace(pending_job_id=order.job_id),
                    self.world.clock(),
                )
                continue
            evidence = RETRYABLE_FAILURE if failed else self._evidence(order)
            for _delivery in range(2 if spark.duplicate_steps else 1):
                try:
                    lifecycle.record_node_result(
                        order.job_id,
                        order.node_id,
                        succeeded=not failed,
                        evidence=evidence,
                    )
                except Exception as error:  # noqa: BLE001 - a refused report is the Controller's answer
                    self.refused.append(f"{order.kind}: {type(error).__name__}")
            if not failed:
                self._apply_effect(spark, order)

    @staticmethod
    def _evidence(order: Order) -> dict[str, object]:
        if order.kind == "recipe.install":
            return {"installed_bytes": 120}
        if order.kind == "recipe.start":
            return start_evidence(order.payload)
        return {}

    def _apply_effect(self, spark: Spark, order: Order) -> None:
        run_id = order.payload.get("run_id")
        if not isinstance(run_id, str):
            return
        if order.kind == "recipe.start":
            spark.serving.add(run_id)
        elif order.kind == "recipe.stop":
            spark.serving.discard(run_id)

    def _report_observations(self) -> None:
        """Each up Spark reports the runs it actually serves."""

        now = self.world.clock()
        with self.world.sessions.begin() as session:
            for spark in self.by_node.values():
                if spark.down or not spark.serving:
                    continue
                for node in session.scalars(
                    select(RunNode).where(
                        RunNode.node_id == spark.node_id,
                        RunNode.run_id.in_(spark.serving),
                    )
                ):
                    node.updated_at = now
                    run = session.get(RecipeRun, node.run_id)
                    if run is None or run.plan.get("observation_schema_version") != 2:
                        continue
                    node.observed_run_generation = run.run_generation
                    node.observation_process_running = True
                    node.observation_observed_at = now
                    node.observation_endpoint_ready = (
                        True if node.role == "entrypoint" else None
                    )
                    node.updated_at = now

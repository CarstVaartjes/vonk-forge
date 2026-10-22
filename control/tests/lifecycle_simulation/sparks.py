"""Fake Spark agents: they report inventory and answer the orders they are given.

Each Spark answers a success through the Controller's own result seam
(``record_node_result``) and a retryable failure through the order core, as the
agent queue takes it. A Spark keeps its own truth of which runs it is serving
and reports their observations, so a Spark that is down stops reporting them; one
that serves nothing sends a complete empty report every round, as the agent does.
Faults are per Spark: down (no inventory, no answers), late (answers held for
some rounds), duplicate (every answer delivered twice), failing (its next
answers are retryable failures: the orders wait and are answered again) and
short of disk (inventory reports almost no free space). A test may model a
Spark's memory: it then reports free memory less what its runs hold.

A result that is lost in transit is the same, to the Controller, as a late one:
the Spark reports it again when it next reaches the Controller. The agent's
claim and lease path is not modelled here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy import select
from vonk_agent_protocol import AgentFailureKind, canonical_message
from vonk_control.inventory_repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.lifecycle import Reported
from vonk_control.lifecycle.agent_operation import AgentOperationAdapter
from vonk_control.memory_reservations import MEMORY_RESERVATION_KINDS
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operations.observation_helpers import (
    prepare_exact_recipe_run_observation_nodes,
)

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


#: Free disk a Spark under pressure reports: below the collector's low mark.
_PRESSURED_FREE_FRACTION = 0.05


@dataclass(frozen=True)
class Memory:
    """A Spark's physical memory, when a test models it."""

    total: int
    #: Free with nothing served.
    idle_free: int
    #: The share of its declared peak a serving run actually holds.
    held_percent: int = 100


@dataclass
class Spark:
    node_id: str
    down_steps: int = 0
    late_rounds: int = 0
    duplicate_steps: int = 0
    pressure_steps: int = 0
    fail_next: bool = False
    #: Its inventory reports stop; it still answers orders and serves.
    inventory_silent: bool = False
    serving: set[str] = field(default_factory=set)
    memory: Memory | None = None

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
        self.fail_next = self.inventory_silent = False


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
        self.inventory = InventoryRepository(world.sessions, clock=world.clock)
        #: Each Spark's own inventory: only the Sparks report it, so what one
        #: last reported is what the Controller holds, without reading it back.
        self.reported = {
            node: self.inventory.latest(node, now=world.clock(), maximum_age=10**9)
            for node in world.nodes
        }
        #: What each Spark reports free when it is not short of disk.
        self.disk_free = {
            node: latest.disk_free_bytes for node, latest in self.reported.items()
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
            if not spark.inventory_silent:
                self._report_inventory(spark)
            if spark.late_rounds > 0:
                spark.late_rounds -= 1
                continue
            self._answer(spark)
        self._report_observations()

    def _report_inventory(self, spark: Spark) -> None:
        now = self.world.clock()
        latest = self.reported[spark.node_id]
        if latest.observed_at >= now:
            return  # already reported at this instant
        free = (
            int(latest.disk_total_bytes * _PRESSURED_FREE_FRACTION)
            if spark.pressure_steps
            else self.disk_free[spark.node_id]
        )
        memory = (
            latest.host_memory_total_bytes,
            latest.host_memory_free_bytes,
            latest.gpu_memory_total_bytes,
            latest.gpu_memory_free_bytes,
        )
        if spark.memory is not None:
            # One shared pool: host and accelerator report the same bytes.
            memory_free = spark.memory.idle_free - self._held(spark)
            memory = (spark.memory.total, memory_free, spark.memory.total, memory_free)
        self.inventory.record(
            NodeInventoryReport.model_validate_json(
                canonical_message(
                    {
                        **latest.model_dump(mode="json"),
                        "observed_at": now,
                        "disk_free_bytes": free,
                        "host_memory_total_bytes": memory[0],
                        "host_memory_free_bytes": memory[1],
                        "gpu_memory_total_bytes": memory[2],
                        "gpu_memory_free_bytes": memory[3],
                    }
                )
            )
        )
        self.reported[spark.node_id] = latest.model_copy(
            update={"observed_at": now, "disk_free_bytes": free}
        )
        with self.world.sessions.begin() as session:
            presence = session.get(AgentNode, spark.node_id)
            if presence is not None:
                presence.management_observed_at = now

    def _held(self, spark: Spark) -> int:
        """What the runs this Spark serves hold: a share of their declared peak."""

        assert spark.memory is not None
        with self.world.sessions() as session:
            declared = session.scalars(
                select(ResourceReservation.amount_bytes).where(
                    ResourceReservation.node_id == spark.node_id,
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id.in_(spark.serving),
                    ResourceReservation.kind.in_(MEMORY_RESERVATION_KINDS),
                )
            ).all()
        return sum(declared) * spark.memory.held_percent // 100

    def _orders(self, node_id: str) -> tuple[Order, ...]:
        with self.world.sessions() as session:
            rows = session.execute(
                select(Job.id, Job.kind, AgentOperation.id, AgentOperation.payload)
                .join(AgentOperation, AgentOperation.parent_job_id == Job.id)
                .where(
                    # A Spark sees its queued orders, whatever their parent shows.
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
            if failed:
                self._wait(order)
                continue
            evidence = self._evidence(order)
            for _delivery in range(2 if spark.duplicate_steps else 1):
                try:
                    lifecycle.record_node_result(
                        order.job_id,
                        order.node_id,
                        succeeded=True,
                        evidence=evidence,
                    )
                except Exception as error:  # noqa: BLE001 - a refused report is the Controller's answer
                    self.refused.append(f"{order.kind}: {type(error).__name__}")
            self._apply_effect(spark, order)

    def _wait(self, order: Order) -> None:
        """A dependency that was briefly unavailable, as the agent queue takes
        it: the failure's own kind decides, so the order waits to be answered
        again and its recipe operation sees no failure. (``record_node_result``
        with ``succeeded=False`` records a definite end, which no Spark's
        retryable failure becomes.)"""

        with self.world.sessions.begin() as session:
            operation = session.get(
                AgentOperation, order.operation_id, with_for_update=True
            )
            if operation is None:
                return
            AgentOperationAdapter(session).settle(
                operation,
                None,
                session.get(Job, order.job_id),
                Reported.failed(
                    AgentFailureKind.TEMPORARY_DEPENDENCY,
                    reason="a runtime dependency was briefly unavailable",
                ),
                self.world.clock(),
            )

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
        """Each up Spark reports the runs it actually serves; as the agent does,
        a Spark serving nothing sends a complete empty report."""

        now = self.world.clock()
        with self.world.sessions.begin() as session:
            for spark in self.by_node.values():
                if spark.down:
                    continue
                if not spark.serving:
                    # What the observations route does with an empty report.
                    prepare_exact_recipe_run_observation_nodes(
                        session, spark.node_id, now, set()
                    )
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

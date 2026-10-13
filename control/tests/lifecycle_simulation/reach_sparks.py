"""The fake Sparks of ``ReachWorld``: agents on the real claim and lease path.

Each up Spark claims its orders from the agent order queue
(``AgentJobService.claim``), does the effect, and reports through the queue
(``record_result``); a report the queue refuses as stale is offered again as a
late result, as the agent API does. A held order is renewed with heartbeats.

Faults of their own, per Spark:

- lost results: the Spark does the effect, but its report never arrives;
- slow work: the Spark holds the order and renews its lease for some rounds
  before it reports;
- stalled work: the Spark holds the order without renewing it for some
  rounds, then reports late;
- a runtime crash: the Spark's serving processes die, and its next report says
  it serves nothing (silence alone never fails a rank);

beside the first Sparks' faults (down, late, duplicate, failing, short of disk).
The model provider being down and the NAS being short of disk are world faults
counted the same way: waiting is legitimate while one is active, and they
count down per step and clear when the world quiesces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from vonk_agent_protocol import AgentResult, canonical_message
from vonk_agent_protocol.runtime_preflight import RuntimePreflightRequest
from vonk_control.agent_jobs import (
    CLAIM_LEASE_SECONDS,
    StaleAgentAttempt,
    _failure_result,
)
from vonk_control.models import AgentNode
from vonk_control.recipe_operations.observation_helpers import (
    prepare_exact_recipe_run_observation_nodes,
)
from vonk_control.recovery_policy import FailureKind
from vonk_control.runtime_preflight import mandatory_capabilities

from ..runtime_identity_support import PACKAGED_RUNTIME_IDENTITY
from ..test_recipe_operations import start_evidence
from .reach_world import ReachWorld
from .sparks import Spark, Sparks
from .world import World

#: The runtime every fake agent reports when it claims.
_RUNTIME = {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}


@dataclass
class Held:
    """An order a Spark has claimed and not reported yet."""

    fence: str
    message: AgentResult
    rounds: int
    renewing: bool


@dataclass
class Leases:
    """One Spark's lease faults, the orders it holds, and a crash to report."""

    crashed: bool = False
    lose_steps: int = 0
    slow_rounds: int = 0
    stall_rounds: int = 0
    held: list[Held] = field(default_factory=list)

    @property
    def faulty(self) -> bool:
        return bool(self.lose_steps or self.slow_rounds or self.stall_rounds)

    def end_step(self) -> None:
        self.lose_steps = max(0, self.lose_steps - 1)

    def clear(self) -> None:
        self.lose_steps = self.slow_rounds = self.stall_rounds = 0


class ReachSparks(Sparks):
    def __init__(self, world: World) -> None:
        assert isinstance(world, ReachWorld)
        super().__init__(world)
        self.leases = {node: Leases() for node in world.nodes}

    @property
    def reach(self) -> ReachWorld:
        assert isinstance(self.world, ReachWorld)
        return self.world

    def healthy(self) -> bool:
        world = self.reach
        return (
            super().healthy()
            and world.provider.down_steps == 0
            and world.nas_short_steps == 0
            and not any(leases.faulty for leases in self.leases.values())
        )

    def end_step(self) -> None:
        super().end_step()
        world = self.reach
        world.provider.down_steps = max(0, world.provider.down_steps - 1)
        world.nas_short_steps = max(0, world.nas_short_steps - 1)
        for leases in self.leases.values():
            leases.end_step()

    def clear_faults(self) -> None:
        super().clear_faults()
        self.reach.provider.down_steps = 0
        self.reach.nas_short_steps = 0
        for leases in self.leases.values():
            leases.clear()

    def renew(self) -> None:
        """Every up Spark renews the leases of the orders it holds and renews."""

        orders = self.reach.reach.orders
        for node_id, leases in self.leases.items():
            if self.by_node[node_id].down:
                continue
            for held in leases.held:
                if not held.renewing:
                    continue
                try:
                    orders.heartbeat(held.fence, None, CLAIM_LEASE_SECONDS)
                except Exception as error:  # noqa: BLE001 - a refused renewal is the queue's answer
                    self.refused.append(f"heartbeat: {type(error).__name__}: {error}")

    def crash(self, index: int) -> None:
        """The Spark's runtime processes die; it reports so at its next poll."""

        spark = self.spark(index)
        spark.serving.clear()
        self.leases[spark.node_id].crashed = True

    def _answer(self, spark: Spark) -> None:
        leases = self.leases[spark.node_id]
        if leases.crashed:
            leases.crashed = False
            with self.world.sessions.begin() as session:
                # A complete report with no run in it: every rank it had is gone.
                prepare_exact_recipe_run_observation_nodes(
                    session, spark.node_id, self.world.clock(), set()
                )
        due = [held for held in leases.held if held.rounds <= 0]
        leases.held = [held for held in leases.held if held.rounds > 0]
        for held in leases.held:
            held.rounds -= 1
        for held in due:
            self._report(spark, held.message)
        self._claim_all(spark, leases)

    def _claim_all(self, spark: Spark, leases: Leases) -> None:
        orders = self.reach.reach.orders
        with self.world.sessions() as session:
            node = session.get(AgentNode, spark.node_id)
            fingerprint = None if node is None else node.preflight_fingerprint
        # Every answer of the round fails, so which order is claimed first
        # (product ids are random) cannot change the outcome.
        failed = spark.fail_next
        claimed = 0
        while claimed < 16:
            claim = orders.claim(
                spark.node_id,
                f"serial-{self.world.nodes.index(spark.node_id)}",
                runtime_identity=_RUNTIME,
                preflight_fingerprint=fingerprint,
            )
            if claim is None:
                break
            claimed += 1
            kind = claim.operation.value
            payload = json.loads(canonical_message(claim.payload))
            message = (
                _failed(claim.fence)
                if failed
                else AgentResult.model_validate(
                    {
                        "fence": claim.fence,
                        "state": "succeeded",
                        "result": self._result(kind, payload),
                    }
                )
            )
            if not failed:
                self._apply_payload(spark, kind, payload)
            if leases.lose_steps:
                continue  # the effect happened; the report never arrives
            if leases.slow_rounds or leases.stall_rounds:
                leases.held.append(
                    Held(
                        claim.fence,
                        message,
                        rounds=leases.slow_rounds or leases.stall_rounds,
                        renewing=bool(leases.slow_rounds),
                    )
                )
                continue
            self._report(spark, message)
        if claimed:
            # The fault covered the orders of this round.
            spark.fail_next = False
            leases.slow_rounds = leases.stall_rounds = 0

    def _report(self, spark: Spark, message: AgentResult) -> None:
        orders = self.reach.reach.orders
        for _delivery in range(2 if spark.duplicate_steps else 1):
            try:
                orders.record_result(message)
            except StaleAgentAttempt:
                # The agent API offers a report the queue no longer runs as late.
                try:
                    orders.record_late_result(message)
                except Exception as error:  # noqa: BLE001 - a refused report is the queue's answer
                    self.refused.append(f"late result: {type(error).__name__}")
            except Exception as error:  # noqa: BLE001 - a refused report is the queue's answer
                self.refused.append(f"result: {type(error).__name__}")

    def _result(self, kind: str, payload: dict[str, object]) -> object:
        if kind == "runtime.preflight.v1":
            request = RuntimePreflightRequest.model_validate(payload)
            return {
                "fingerprint": "a" * 64,
                "observed_at": int(self.world.clock().timestamp()),
                "findings": [
                    {"capability": capability, "status": "passed", "code": "available"}
                    for capability in mandatory_capabilities(request)
                ],
            }
        if kind == "recipe.install":
            return {"installed_bytes": 120}
        if kind == "recipe.start":
            return start_evidence(payload)
        return {}

    @staticmethod
    def _apply_payload(spark: Spark, kind: str, payload: dict[str, object]) -> None:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str):
            return
        if kind == "recipe.start":
            spark.serving.add(run_id)
        elif kind == "recipe.stop":
            spark.serving.discard(run_id)


def _failed(fence: str) -> AgentResult:
    """A dependency was briefly unavailable: a retryable failure."""

    return AgentResult.model_validate(
        {
            "fence": fence,
            "state": "failed",
            "result": _failure_result(
                "runtime_observation_unavailable",
                "a runtime dependency was briefly unavailable",
                uncertain=False,
                failure_kind=FailureKind.TEMPORARY_DEPENDENCY,
            ),
        }
    )

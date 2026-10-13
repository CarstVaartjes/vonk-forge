"""Liveness properties of the parts ``ReachWorld`` adds, beside the first set.

- A removal gate never outlives its owner: a gate whose removal operation (or
  Job) ended or is gone is released within the claim grace.
- A model-cache operation ends ``failed`` only for a reason outside the
  Controller: a download while the provider was down at some point in its
  life. A removal has nothing outside the Controller to fail on; it either
  removes the model or waits, naming what still uses it.
- A run that lost a rank is recovered or ended: it does not stay running with
  a failed rank for the stall window while every fault is clear.
- A run that ended leaves no rank running: the Controller stops what its own
  records say still runs, within the stall window while every fault is clear.
- An order whose lease lapsed does not stay running: the queue expires the
  attempt and reissues or ends the order within the recovery budget.
- A load ends ``failed`` only when a fault (a Spark, the provider, the NAS or
  a paused Controller) was active at some point while it was live.

A violation whose signature matches a known bug is set aside and the run goes
on, so a seed that meets a known bug early still explores what comes after it;
any other violation ends the run at once. ``tolerated`` keeps the first one of
each known signature for the caller to report.

The model cache's operations, sets and gates join the world's state, so a load
waiting behind a download that progresses is not counted as stuck.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import select
from vonk_control.agent_operation_facts import aware as _aware
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactLifecycleGate,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeRun,
    RunNode,
)

from .invariants import (
    CLAIM_GRACE,
    ENDED,
    STALL_WINDOW,
    Monitor,
    Violation,
    _Snapshot,
)
from .reach_world import ReachWorld

#: An order may keep running on a lapsed lease this long: the queue's recovery
#: budget (a start inside its launch budget may renew late), and a margin.
LAPSED_LEASE_WINDOW = timedelta(hours=1, minutes=5)
_GATE_OWNERS = {"model-cache-operation": ModelCacheOperation, "recipe-image-job": Job}


@dataclass
class ReachMonitor(Monitor):
    #: Patterns of known bugs' signatures (see ``Violation.signature``).
    known: Sequence[str] = ()
    #: The first violation of each known signature met so far.
    tolerated: list[Violation] = field(default_factory=list)
    _gates_dead_since: dict[str, datetime] = field(default_factory=dict)
    #: Cache operations and loads that were live while a fault was active.
    _faulted: set[str] = field(default_factory=set)
    _states: dict[str, str] = field(default_factory=dict)
    _rank_lost_since: dict[str, datetime] = field(default_factory=dict)
    _orphan_since: dict[str, datetime] = field(default_factory=dict)

    def is_known(self, violation: Violation) -> bool:
        return any(re.match(pattern, violation.signature) for pattern in self.known)

    def set_aside(self, violation: Violation) -> None:
        signatures = {seen.signature for seen in self.tolerated}
        if violation.signature not in signatures:
            self.tolerated.append(violation)

    @property
    def reach(self) -> ReachWorld:
        assert isinstance(self.world, ReachWorld)
        return self.world

    def snapshot(self) -> _Snapshot:
        current = super().snapshot()
        extra: set[tuple[str, ...]] = set()
        with self.world.sessions() as session:
            for operation in session.scalars(select(ModelCacheOperation)):
                extra.add(
                    (
                        "cache",
                        operation.id,
                        operation.state,
                        str(operation.last_error),
                        str(operation.progress.get("phase")),
                    )
                )
            for cached in session.scalars(select(ModelCacheSet)):
                extra.add(("set", cached.artifact_set_sha256, cached.state))
            for rank in session.scalars(select(RunNode)):
                extra.add(("rank", rank.run_id, rank.node_id, rank.state))
            for gate in session.scalars(select(ArtifactLifecycleGate)):
                extra.add(
                    (
                        "gate",
                        gate.artifact_kind,
                        gate.artifact_sha256,
                        str(gate.removal_owner_id),
                    )
                )
        return replace(current, core=current.core | frozenset(extra))

    def observe(self) -> list[Violation]:
        violations = super().observe()
        now = self.world.clock()
        violations += self._gates(now)
        violations += self._failures()
        violations += self._lapsed_leases(now)
        violations += self._lost_ranks(now)
        unknown = []
        for violation in violations:
            if self.is_known(violation):
                self.set_aside(violation)
            else:
                unknown.append(violation)
        return unknown

    def _gates(self, now: datetime) -> list[Violation]:
        dead: dict[str, str] = {}
        with self.world.sessions() as session:
            for gate in session.scalars(
                select(ArtifactLifecycleGate).where(
                    ArtifactLifecycleGate.removal_owner_id.is_not(None)
                )
            ):
                model = _GATE_OWNERS.get(str(gate.removal_owner_kind))
                if model is None:
                    continue
                owner = session.get(model, gate.removal_owner_id)
                if owner is not None and owner.state not in ENDED:
                    continue
                state = "missing" if owner is None else owner.state
                key = f"{gate.artifact_kind}:{gate.artifact_sha256}"
                dead[key] = (
                    f"{gate.artifact_kind} gate {gate.artifact_sha256[:12]} held by"
                    f" {state} {gate.removal_owner_kind} {gate.removal_owner_id}"
                )
        self._gates_dead_since = {
            key: self._gates_dead_since.get(key, now) for key in dead
        }
        return [
            Violation("no removal gate outlives its owner", dead[key])
            for key, since in sorted(self._gates_dead_since.items())
            if now - since > CLAIM_GRACE
        ]

    def _lost_ranks(self, now: datetime) -> list[Violation]:
        """Runs still running with a failed rank, and ended runs still running one."""

        with self.world.sessions() as session:
            runs = set(
                session.scalars(
                    select(RecipeRun.id)
                    .join(RunNode, RunNode.run_id == RecipeRun.id)
                    .where(RecipeRun.state == "running", RunNode.state == "failed")
                )
            )
            orphans = {
                f"{run_id} rank on {node_id} {state}"
                for run_id, node_id, state in session.execute(
                    select(RunNode.run_id, RunNode.node_id, RunNode.state)
                    .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                    .where(
                        RecipeRun.state.in_(("failed", "stopped")),
                        RunNode.state.in_(("running", "starting")),
                    )
                )
            }
        healthy = self.sparks.healthy()
        self._rank_lost_since = {
            run: self._rank_lost_since.get(run, now) if healthy else now for run in runs
        }
        self._orphan_since = {
            rank: self._orphan_since.get(rank, now) if healthy else now
            for rank in orphans
        }
        return [
            Violation(
                "a run that lost a rank is recovered or ended",
                f"run {run} running with a failed rank since {since}",
            )
            for run, since in sorted(self._rank_lost_since.items())
            if now - since > STALL_WINDOW
        ] + [
            Violation(
                "a run that ended leaves no rank running",
                f"ended run {rank} since {since}",
            )
            for rank, since in sorted(self._orphan_since.items())
            if now - since > STALL_WINDOW
        ]

    def _lapsed_leases(self, now: datetime) -> list[Violation]:
        with self.world.sessions() as session:
            rows = session.execute(
                select(
                    AgentOperation.id,
                    AgentOperation.kind,
                    AgentOperationAttempt.lease_deadline,
                )
                .join(
                    AgentOperationAttempt,
                    (AgentOperationAttempt.operation_id == AgentOperation.id)
                    & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
                )
                .where(AgentOperation.state == "running")
                .order_by(AgentOperation.id)
            ).all()
        return [
            Violation(
                "an order whose lease lapsed is reclaimed",
                f"{kind} order {operation_id} running on a lease that lapsed at"
                f" {deadline}",
            )
            for operation_id, kind, deadline in rows
            if deadline is not None and now - _aware(deadline) > LAPSED_LEASE_WINDOW
        ]

    def _failures(self) -> list[Violation]:
        """Cache operations and loads that failed with no fault to blame."""

        # A paused Controller (a clock jump) is a fault outside it too.
        faulty = not self.sparks.healthy() or self.reach.paused
        self.reach.paused = False
        with self.world.sessions() as session:
            ended = [
                (f"{kind} {row_id}", kind != "remove", state, error)
                for row_id, kind, state, error in session.execute(
                    select(
                        ModelCacheOperation.id,
                        ModelCacheOperation.kind,
                        ModelCacheOperation.state,
                        ModelCacheOperation.last_error,
                    ).order_by(ModelCacheOperation.created_at, ModelCacheOperation.id)
                )
            ]
            loads = session.execute(
                select(
                    FleetProfileApplication.id,
                    FleetProfileApplication.state,
                    FleetProfileApplication.status_reason,
                    FleetProfileApplication.created_at,
                ).order_by(
                    FleetProfileApplication.created_at,
                    FleetProfileApplication.request_key,
                )
            ).all()
            # A load a newer one replaced may end failed: newer intent won.
            ended += [
                (f"load {row_id}", True, state, reason)
                for row_id, state, reason, accepted in loads
                if state != "failed"
                or sum(other >= accepted for *_row, other in loads) == 1
            ]
        violations: list[Violation] = []
        for subject, may_fault, state, reason in ended:
            before = self._states.get(subject)
            self._states[subject] = state
            if state not in ENDED and faulty:
                self._faulted.add(subject)
            if state != "failed" or before == "failed":
                continue
            if may_fault and subject in self._faulted:
                continue
            violations.append(
                Violation(
                    "nothing fails without a fault outside the Controller",
                    f"{subject} failed: {reason}",
                )
            )
        return violations

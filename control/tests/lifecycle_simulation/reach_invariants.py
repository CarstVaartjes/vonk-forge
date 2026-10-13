"""Liveness properties of the parts ``ReachWorld`` adds, beside the first set.

- A removal gate never outlives its owner: a gate whose removal operation (or
  Job) ended or is gone is released within the claim grace.
- A model-cache operation ends ``failed`` only for a reason outside the
  Controller: a download while the provider was down at some point in its
  life. A removal has nothing outside the Controller to fail on; it either
  removes the model or waits, naming what still uses it.
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
from datetime import datetime

from sqlalchemy import select
from vonk_control.models import (
    ArtifactLifecycleGate,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
)

from .invariants import CLAIM_GRACE, ENDED, Monitor, Violation, _Snapshot
from .reach_world import ReachWorld

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
            ended += [
                (f"load {row_id}", True, state, reason)
                for row_id, state, reason in session.execute(
                    select(
                        FleetProfileApplication.id,
                        FleetProfileApplication.state,
                        FleetProfileApplication.status_reason,
                    ).order_by(
                        FleetProfileApplication.created_at,
                        FleetProfileApplication.request_key,
                    )
                )
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

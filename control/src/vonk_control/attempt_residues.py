"""Release what a finished attempt left behind, by one rule.

A profile load, Run/Switch or cleanup leaves durable per-attempt records:
installation plans with their disk claims, and node distribution grants. Each
belongs to the operation that created it while that operation is active. When
no active operation owns the record any more (it failed, was cancelled or was
superseded, possibly by a crash), the record is a residue:

* a residue with **no proven effect on a Spark** is released here, the same
  way whichever attempt left it behind;
* a residue that **may have an effect** (installed or partially installed
  files, a running workload) is never released directly here. A leftover
  *incomplete installation* (failed, partial, or installed with a rank not
  installed or short of its payload) that nothing owns (no active operation on
  its Sparks, no run that has not stopped) and that nobody has touched for the
  retention grace, or that a later installation of the same recipe on the same
  Sparks superseded, is queued to the lifecycle's own uninstall, the same path
  the retention sweep uses. Until then, and when the uninstall is refused (for
  example because its rank membership no longer matches the plan), the fleet
  view names it (``install.partial`` with typed evidence) instead of a silent
  wait.

Separately, a *reservation* (disk, port, memory) is bookkeeping and not an
effect: when its owner is dead (a cancelled or failed profile application, a
stopped run, an installation with no operation issued for it, a build nothing
runs, or an owner that no longer exists) it is released by owner state, see
``reservation_owners``. The installation row and the files it wrote stay for
the cleanup path; what is on the Spark is what its next inventory reports.

Ownership is decided conservatively and without owner columns: a record is
owned while any active operation targets one of its Sparks (or, for a grant,
names its plan).

An unowned installation plan is first **adoptable**: the next attempt of the
same recipe on the same Sparks reuses it (same plan digest), and its admission
does not count the plan's disk claim as capacity to wait for, so adoption is
never gated behind a cleanup wait for the very record it adopts. Only a plan
that stays unowned for the adoption window, because no retry came, is
released. A plan a later installation of the same recipe on the same Sparks
has **superseded** is released without that wait: the next attempt already
planned differently (another revision, image or choice, so another digest),
and nothing practical can adopt the older plan any more. Every release is logged with its
kind, id and reason.

Deliberately not residues: model-cache downloads (idempotent and useful to
finish), runtime image authorizations and receipts (content-addressed and
reused by design), cluster mappings (immutable placements), and agent
operations (the agent job service retires them).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    RunState,
    UnknownOutcomeError,
)

from . import job_states
from .fleet_projection import _installation_payload_expectations
from .logging import log_event
from .machine_states import DISTRIBUTION_HELD
from .models import (
    ArtifactDistributionAssignment,
    CatalogDocumentRevision,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from .recipe_action_plans import UninstallPlan
from .reservation_owners import (
    ADOPTION_WINDOW,
    release_covered_installation_claims,
    release_dead_owner_reservations,
)

_LOGGER = logging.getLogger(__name__)
#: How long an unowned incomplete installation is left alone before it is
#: removed (the retention grace: a retry or a person may still be using it).
LEFTOVER_GRACE = timedelta(hours=24)
_LEFTOVER_ACTOR = "system:residue-sweep"
_ACTIVE_JOB_STATES = job_states.words(
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    LifecycleState.OBSERVING,
    LifecycleState.NEEDS_OPERATOR,
)
# Operations that may own an installation plan or a distribution grant.
_OWNER_JOB_KINDS = (
    "recipe.run-switch.v2",
    "recipe.stop.v2",
    "recipe.cleanup.v2",
    "artifact-distribution",
    "recipe.install",
    "recipe.uninstall",
    "recipe.reconcile",
    "recipe.start",
)


def _owned_nodes(session: Session) -> set[str]:
    return {
        node_id
        for targets in session.scalars(
            select(Job.targets).where(
                Job.kind.in_(_OWNER_JOB_KINDS), Job.state.in_(_ACTIVE_JOB_STATES)
            )
        )
        if isinstance(targets, list)
        for node_id in targets
        if isinstance(node_id, str)
    }


def unowned_never_installed(
    session: Session,
    *,
    recipe_revision_id: str | None = None,
    node_ids: Collection[str] | None = None,
) -> tuple[str, ...]:
    """Installation plans no active operation owns and no node ran.

    The one definition shared by admission (which adopts them) and the
    reconciler (which releases them once nobody did). With ``recipe_revision_id``
    and ``node_ids`` it names only plans an attempt of that recipe on exactly
    those Sparks would adopt.
    """

    statement = select(RecipeInstallation).where(
        RecipeInstallation.state == InstallationState.PLANNED
    )
    if recipe_revision_id is not None:
        statement = statement.where(
            RecipeInstallation.recipe_revision_id == recipe_revision_id
        )
    owned = _owned_nodes(session)
    wanted = frozenset(node_ids) if node_ids is not None else None
    found: list[str] = []
    for installation in session.scalars(statement):
        members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                )
            )
        )
        member_nodes = {member.node_id for member in members}
        if (
            members
            and not owned.intersection(member_nodes)
            and (wanted is None or member_nodes == wanted)
            and all(
                member.state == InstallationNodeState.PLANNED
                and not member.installed_bytes
                for member in members
            )
        ):
            found.append(installation.id)
    return tuple(found)


def superseded_plan_ids(
    session: Session, installation_ids: Collection[str]
) -> frozenset[str]:
    """Plans a later installation of the same recipe and Sparks replaced.

    A plan is adopted only by an attempt whose plan digest matches, and the
    digest names the recipe revision and image. Once a later installation of
    the same recipe (any revision) exists on exactly the same Sparks, the
    earlier plan has no practical adopter: profiles follow the newest
    revision. It would otherwise outlive the adoption window whenever attempts
    follow each other on those Sparks, and show as a second installation of
    one recipe. Releasing it is always safe, since a plan that never reached a
    Spark costs only its next re-plan.
    """

    superseded: set[str] = set()
    for installation_id in installation_ids:
        installation = session.get(RecipeInstallation, installation_id)
        if installation is None:
            continue
        nodes = frozenset(
            session.scalars(
                select(InstallationNode.node_id).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        same_recipe = select(CatalogDocumentRevision.id).where(
            CatalogDocumentRevision.document_id
            == select(CatalogDocumentRevision.document_id)
            .where(CatalogDocumentRevision.id == installation.recipe_revision_id)
            .scalar_subquery()
        )
        later = session.scalars(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_revision_id.in_(same_recipe),
                RecipeInstallation.id != installation_id,
                RecipeInstallation.state != InstallationState.UNINSTALLED,
                RecipeInstallation.created_at > installation.created_at,
            )
        )
        for other in later:
            other_nodes = frozenset(
                session.scalars(
                    select(InstallationNode.node_id).where(
                        InstallationNode.installation_id == other.id
                    )
                )
            )
            if other_nodes == nodes:
                superseded.add(installation_id)
                break
    return frozenset(superseded)


class LeftoverRemoval(Protocol):
    """The recipe lifecycle's uninstall, as the residue sweep uses it."""

    def preview_uninstall(self, installation_id: str) -> UninstallPlan: ...

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
    ) -> object: ...


def incomplete_installation_ids(session: Session) -> tuple[str, ...]:
    """Installations whose group is incomplete, by the fleet view's own rule.

    Failed or partial installations, and installed ones with a rank that is not
    installed or is short of the admitted payload.
    """

    found: list[str] = []
    for installation in session.scalars(
        select(RecipeInstallation).where(
            RecipeInstallation.state.in_(
                (
                    InstallationState.INSTALLED,
                    InstallationState.PARTIAL,
                    InstallationState.FAILED,
                )
            )
        )
    ):
        if installation.state != InstallationState.INSTALLED:
            found.append(installation.id)
            continue
        expectations = _installation_payload_expectations(installation.plan)
        if any(
            node.state != InstallationNodeState.INSTALLED
            or (
                node.node_id in expectations
                and node.installed_bytes < expectations[node.node_id]
            )
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                )
            )
        ):
            found.append(installation.id)
    return tuple(found)


class AttemptResidueReconciler:
    """One sweep, one rule, for every per-attempt record kind."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        abandon_never_installed: Callable[[str], object],
        removal: LeftoverRemoval | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._abandon = abandon_never_installed
        self._removal = removal
        self._clock = clock
        self._retry_at: dict[str, datetime] = {}

    def tick(self) -> bool:
        """Release every current residue; True when anything was released."""

        now = self._clock()
        self._retry_at = {key: due for key, due in self._retry_at.items() if now < due}
        released = self._never_installed_plans()
        released = self._incomplete_installations() or released
        released = self._dead_owner_claims() or released
        return self._ownerless_grants() or released

    def _owned_scope(self, session: Session) -> tuple[set[str], set[str]]:
        """Sparks and plan digests an active operation still owns."""

        nodes: set[str] = set()
        plans: set[str] = set()
        for job in session.scalars(
            select(Job).where(
                Job.kind.in_(_OWNER_JOB_KINDS), Job.state.in_(_ACTIVE_JOB_STATES)
            )
        ):
            nodes.update(job.targets if isinstance(job.targets, list) else ())
            plans.add(job.authority_revision)
            payload = job.payload if isinstance(job.payload, dict) else {}
            digest = payload.get("plan_digest")
            if isinstance(digest, str):
                plans.add(digest)
        return nodes, plans

    def _never_installed_plans(self) -> bool:
        """Release plans nobody adopted within the adoption window."""

        now = self._clock()
        with self._sessions() as session:
            candidates = []
            unowned = unowned_never_installed(session)
            superseded = superseded_plan_ids(session, unowned)
            for installation_id in unowned:
                nodes = set(
                    session.scalars(
                        select(InstallationNode.node_id).where(
                            InstallationNode.installation_id == installation_id
                        )
                    )
                )
                if installation_id not in superseded and not self._quiet_since(
                    session, nodes, now - ADOPTION_WINDOW
                ):
                    continue  # a retry may still adopt it
                candidates.append(installation_id)
        released = False
        for installation_id in candidates:
            if self._clock() < self._retry_at.get(installation_id, self._clock()):
                continue
            try:
                # The lifecycle re-derives "never reached a node" under the
                # installation row lock, so a concurrent install refuses this.
                self._abandon(installation_id)
            except UnknownOutcomeError as error:
                self._defer(installation_id, error)
                continue
            except (
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
                SQLAlchemyError,
            ) as error:  # one residue must not stop the sweep
                self._log("installation-plan", installation_id, "refused", error)
                continue
            self._log(
                "installation-plan",
                installation_id,
                "released",
                reason="superseded" if installation_id in superseded else "unadopted",
            )
            released = True
        return released

    def _leftover_candidates(self) -> list[tuple[str, str]]:
        """Unowned incomplete installations that are due, with why."""

        now = self._clock()
        due: list[tuple[str, str]] = []
        with self._sessions() as session:
            incomplete = incomplete_installation_ids(session)
            if not incomplete:
                return []
            owned = _owned_nodes(session)
            superseded = superseded_plan_ids(session, incomplete)
            for installation_id in incomplete:
                nodes = set(
                    session.scalars(
                        select(InstallationNode.node_id).where(
                            InstallationNode.installation_id == installation_id
                        )
                    )
                )
                if not nodes or owned.intersection(nodes):
                    continue
                if session.scalar(
                    select(RecipeRun.id)
                    .where(
                        RecipeRun.installation_id == installation_id,
                        RecipeRun.state != RunState.STOPPED,
                    )
                    .limit(1)
                ):
                    continue
                if installation_id in superseded:
                    due.append((installation_id, "superseded"))
                elif self._quiet_since(session, nodes, now - LEFTOVER_GRACE) and (
                    self._untouched_since(
                        session, installation_id, now - LEFTOVER_GRACE
                    )
                ):
                    due.append((installation_id, "unowned-past-grace"))
        return due

    @staticmethod
    def _untouched_since(
        session: Session, installation_id: str, since: datetime
    ) -> bool:
        installation = session.get(RecipeInstallation, installation_id)
        if installation is None:
            return False
        stamps = [installation.updated_at]
        stamps.extend(
            session.scalars(
                select(InstallationNode.updated_at).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        return all(
            (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)) < since
            for stamp in stamps
        )

    def _incomplete_installations(self) -> bool:
        """Queue the lifecycle's uninstall for leftover incomplete installations.

        The uninstall re-proves, under its own locks, that nothing runs or is
        in flight on those Sparks; a refusal (blocked plan, changed
        membership, a fault) keeps the installation for the next tick, and the
        fleet view keeps naming it.
        """

        if self._removal is None:
            return False
        queued = False
        for installation_id, why in self._leftover_candidates():
            if self._clock() < self._retry_at.get(installation_id, self._clock()):
                continue
            try:
                plan = self._removal.preview_uninstall(installation_id)
                if not plan.allowed:
                    code = plan.blockers[0].code if plan.blockers else "not allowed"
                    self._log(
                        "incomplete-installation",
                        installation_id,
                        "kept",
                        reason=str(code),
                    )
                    continue

                def still_unowned(
                    session: Session, owner: str = installation_id
                ) -> None:
                    nodes = set(
                        session.scalars(
                            select(InstallationNode.node_id).where(
                                InstallationNode.installation_id == owner
                            )
                        )
                    )
                    if _owned_nodes(session).intersection(nodes):
                        raise RuntimeError("an operation now owns these Sparks")

                self._removal.uninstall(
                    installation_id,
                    plan_digest=plan.plan_digest,
                    actor=_LEFTOVER_ACTOR,
                    request_id=str(uuid.uuid4()),
                    unattended_guard=still_unowned,
                )
            except UnknownOutcomeError as error:
                self._defer(installation_id, error)
                continue
            except (
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
                SQLAlchemyError,
            ) as error:  # a conflict (RecipeOperationConflict) is a RuntimeError
                self._log("incomplete-installation", installation_id, "refused", error)
                continue
            self._log(
                "incomplete-installation", installation_id, "released", reason=why
            )
            queued = True
        return queued

    def _defer(self, installation_id: str, error: UnknownOutcomeError) -> None:
        due = self._clock() + timedelta(seconds=5)
        self._retry_at[installation_id] = due
        self._log(
            "installation",
            installation_id,
            "deferred",
            error,
            next_attempt_at=due.isoformat(),
        )

    @staticmethod
    def _quiet_since(session: Session, nodes: set[str], since: datetime) -> bool:
        """Whether no operation on these Sparks changed since ``since``."""

        for targets in session.scalars(
            select(Job.targets).where(
                Job.kind.in_(_OWNER_JOB_KINDS), Job.updated_at >= since
            )
        ):
            if isinstance(targets, list) and nodes.intersection(targets):
                return False
        return True

    def _dead_owner_claims(self) -> bool:
        """Release disk, port and memory claims whose owner can no longer use them.

        Whichever path ended the owner (a state written directly, a crash, a
        cancel) this sweep finds the claim by its owner's state. It never waits
        for a row another transaction holds, and a fault here only defers it to
        the next tick.
        """

        now = self._clock()
        try:
            with self._sessions.begin() as session:
                released = (
                    *release_dead_owner_reservations(session, now),
                    *release_covered_installation_claims(session, now),
                )
        except SQLAlchemyError as error:
            self._log("reservation", "sweep", "refused", error)
            return False
        for owner in released:
            self._log(
                "reservation",
                owner.owner_id,
                "released",
                owner=owner.describe(),
                owner_kind=owner.kind,
            )
        return bool(released)

    def _ownerless_grants(self) -> bool:
        """Revoke node distribution grants whose plan no operation owns."""

        now = self._clock()
        revoked: list[str] = []
        with self._sessions.begin() as session:
            _nodes, owned_plans = self._owned_scope(session)
            for grant in session.scalars(
                select(ArtifactDistributionAssignment).where(
                    ArtifactDistributionAssignment.state.in_(DISTRIBUTION_HELD)
                )
            ):
                if grant.plan_digest in owned_plans:
                    continue
                # A later transfer of the same plan re-registers (and so
                # reclaims) a revoked grant; nothing serves this one meanwhile.
                grant.state = "revoked"
                grant.revoked_at = now
                grant.updated_at = now
                revoked.append(grant.id)
        for grant_id in revoked:
            self._log("distribution-grant", grant_id, "released")
        return bool(revoked)

    @staticmethod
    def _log(
        kind: str,
        record_id: str,
        outcome: str,
        error: Exception | None = None,
        **context: str,
    ) -> None:
        log_event(
            _LOGGER,
            f"attempt_residue.{outcome}",
            service="control-worker",
            kind=kind,
            record_id=record_id,
            **context,
            **(
                {
                    "code": getattr(error, "code", type(error).__name__),
                    "detail": str(error)[:256],
                }
                if error is not None
                else {}
            ),
        )


__all__ = ["AttemptResidueReconciler"]

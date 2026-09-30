"""Release what a finished attempt left behind, by one rule.

A profile load, Run/Switch or cleanup leaves durable per-attempt records:
installation plans with their disk claims, and node distribution grants. Each
belongs to the operation that created it while that operation is active. When
no active operation owns the record any more (it failed, was cancelled or was
superseded, possibly by a crash), the record is a residue:

* a residue with **no proven effect on a Spark** is released here, the same
  way whichever attempt left it behind;
* a residue that **may have an effect** (installed or partially installed
  files, a running workload) is never released here: the exact
  reconcile/cleanup path owns it, and the fleet view reports it
  (``install.partial``) instead of a silent wait.

Ownership is decided conservatively and without owner columns: a record is
owned while any active operation targets one of its Sparks (or, for a grant,
names its plan).

An unowned installation plan is first **adoptable**: the next attempt of the
same recipe on the same Sparks reuses it (same plan digest), and its admission
does not count the plan's disk claim as capacity to wait for, so adoption is
never gated behind a cleanup wait for the very record it adopts. Only a plan
that stays unowned for the adoption window, because no retry came, is
released. Every release is logged with its kind, id and reason.

Deliberately not residues: model-cache downloads (idempotent and useful to
finish), runtime image authorizations and receipts (content-addressed and
reused by design), cluster mappings (immutable placements), and agent
operations (the agent job service retires them).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .agent_jobs import release_owned_reservations_in_session
from .logging import log_event
from .models import (
    ArtifactDistributionAssignment,
    InstallationNode,
    Job,
    RecipeInstallation,
    ResourceReservation,
)

_LOGGER = logging.getLogger(__name__)
# How long an unowned plan waits for the next attempt to adopt it.
ADOPTION_WINDOW = timedelta(minutes=15)
_ACTIVE_JOB_STATES = ("queued", "running", "waiting", "waiting-for-operator")
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

    statement = select(RecipeInstallation).where(RecipeInstallation.state == "planned")
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
                member.state == "planned" and not member.installed_bytes
                for member in members
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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._abandon = abandon_never_installed
        self._clock = clock

    def tick(self) -> bool:
        """Release every current residue; True when anything was released."""

        released = self._never_installed_plans()
        released = self._stale_installation_claims() or released
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
            for installation_id in unowned_never_installed(session):
                nodes = set(
                    session.scalars(
                        select(InstallationNode.node_id).where(
                            InstallationNode.installation_id == installation_id
                        )
                    )
                )
                if not self._quiet_since(session, nodes, now - ADOPTION_WINDOW):
                    continue  # a retry may still adopt it
                candidates.append(installation_id)
        released = False
        for installation_id in candidates:
            try:
                # The lifecycle re-derives "never reached a node" under the
                # installation row lock, so a concurrent install refuses this.
                self._abandon(installation_id)
            except (
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
                SQLAlchemyError,
            ) as error:  # one residue must not stop the sweep
                self._log("installation-plan", installation_id, "refused", error)
                continue
            self._log("installation-plan", installation_id, "released")
            released = True
        return released

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

    def _stale_installation_claims(self) -> bool:
        """Release installation-owned claims whose installation is gone."""

        now = self._clock()
        released: list[str] = []
        with self._sessions.begin() as session:
            owners = set(
                session.scalars(
                    select(ResourceReservation.owner_id).where(
                        ResourceReservation.owner_kind == "installation",
                        ResourceReservation.state == "active",
                    )
                )
            )
            for owner_id in sorted(owners):
                installation = session.get(RecipeInstallation, owner_id)
                if installation is None or installation.state == "uninstalled":
                    release_owned_reservations_in_session(
                        session, "installation", owner_id, now
                    )
                    released.append(owner_id)
        for owner_id in released:
            self._log("installation-claim", owner_id, "released")
        return bool(released)

    def _ownerless_grants(self) -> bool:
        """Revoke node distribution grants whose plan no operation owns."""

        now = self._clock()
        revoked: list[str] = []
        with self._sessions.begin() as session:
            _nodes, owned_plans = self._owned_scope(session)
            for grant in session.scalars(
                select(ArtifactDistributionAssignment).where(
                    ArtifactDistributionAssignment.state.in_(("active", "expired"))
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
        kind: str, record_id: str, outcome: str, error: Exception | None = None
    ) -> None:
        log_event(
            _LOGGER,
            f"attempt_residue.{outcome}",
            service="control-worker",
            kind=kind,
            record_id=record_id,
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

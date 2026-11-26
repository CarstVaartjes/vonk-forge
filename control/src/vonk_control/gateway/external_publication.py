"""Which external models a gateway generation serves, and what its acknowledgement settles.

The route service builds every generation from SQL, never from the last bundle, so a
removed entry cannot come back.  :func:`plan_externals` is the external half of that
candidate: it moves each deployment along its state machine
(:mod:`vonk_control.gateway.external_states`) and returns the entries to render.
:func:`record_externals` settles the plan once the supervisor acknowledged the
generation (an introduced entry is now applied, a withdrawn one is gone), and
:func:`block_introduced` is the failure path: an acknowledgement that never came
blames exactly the entries the generation introduced, so the next plan serves the
local routes without them.

Nothing here touches the network or a file but through its arguments, and every
change is idempotent: a plan made twice in one transaction, or again after a lost
response, writes nothing the first did not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_words import Disposition
from vonk_agent_protocol.lifecycle_vocabulary import OperatorActionName, WaitReason
from vonk_agent_protocol.state_machines import (
    ExternalBlockReason,
    ExternalPublicationState,
)
from vonk_control.gateway.external_contract import (
    CredentialBinding,
    ExternalDeploymentSpec,
    ExternalRouteEntry,
)
from vonk_control.gateway.external_states import (
    ExternalEvent,
    apply_event,
    settle,
)
from vonk_control.gateway.external_validation import destination_block
from vonk_control.gateway.litellm import render_external_entry
from vonk_control.gateway.route_bundle_contract import RouteExternalIdentity
from vonk_control.models.gateway import (
    GatewayCredentialBinding,
    GatewayExternalDeployment,
)
from vonk_control.recovery_policy import DISTRIBUTED_RECOVERY, ROUTE_RECOVERY, schedule
from vonk_control.waits.persisted import park_wait, schedule_wait

#: The owner's identical-exit rule: after this many failed starts of the same entry
#: content the deployment stays blocked with evidence and stops retrying; an edit
#: (a content change) or an enable starts again.  Every try restarts the gateway.
START_FAILURE_LIMIT = 3

#: A start that failed is looked at again at the shared policy's cap: every try
#: restarts the gateway, so it settles at the slowest step at once.
_START_FAILED_ATTEMPTS = 8

#: The loaded credential names, or ``None`` when the supervisor has not said.
CredentialNames = Callable[[], Collection[str] | None]


@dataclass(frozen=True)
class ServedExternal:
    """One entry of a generation: its deployment, revision and rendered form."""

    deployment_id: str
    revision: int
    spec: ExternalDeploymentSpec
    entry: ExternalRouteEntry
    #: The desired spec, not yet applied: the generation adds or changes it.
    introduced: bool

    def identity(self) -> RouteExternalIdentity:
        return RouteExternalIdentity(
            deployment_id=self.deployment_id,
            revision=self.revision,
            entry_sha256=hashlib.sha256(
                self.entry.model_dump_json(exclude_none=True).encode()
            ).hexdigest(),
        )


@dataclass(frozen=True)
class ExternalPlan:
    """The external entries one generation serves and the deployments it withdraws."""

    served: tuple[ServedExternal, ...] = ()
    #: Deployments with an applied entry that this generation leaves out.
    withdrawing: frozenset[str] = frozenset()

    @property
    def entries(self) -> tuple[ExternalRouteEntry, ...]:
        return tuple(item.entry for item in self.served)

    @property
    def introduced(self) -> tuple[ServedExternal, ...]:
        return tuple(item for item in self.served if item.introduced)

    def identities(self) -> list[RouteExternalIdentity] | None:
        """The identity of the entries, or ``None`` when there are none.

        ``None`` keeps the digest of a gateway without external models exactly as
        it was, so deploying this release restarts nothing.
        """

        return [item.identity() for item in self.served] or None


def _aware(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)


def read_bindings(session: Session) -> Mapping[str, CredentialBinding]:
    """The approved destinations by binding name; an unreadable record approves nothing."""

    found: dict[str, CredentialBinding] = {}
    for row in session.scalars(select(GatewayCredentialBinding)):
        destinations = row.destinations
        if isinstance(destinations, list):
            found[row.name] = CredentialBinding(
                name=row.name, revision=row.revision, destinations=destinations
            )
    return found


def _spec(stored: object) -> ExternalDeploymentSpec | None:
    """The spec a column holds; a damaged document (a residue) reads as no spec."""

    return stored if isinstance(stored, ExternalDeploymentSpec) else None


def _serve(
    row: GatewayExternalDeployment,
    spec: ExternalDeploymentSpec,
    revision: int,
    *,
    introduced: bool,
) -> ServedExternal | ExternalBlockReason:
    try:
        entry = render_external_entry(spec, row.id, revision)
    except ValidationError:
        return ExternalBlockReason.ENTRY_INVALID
    return ServedExternal(row.id, revision, spec, entry, introduced=introduced)


def _block(
    row: GatewayExternalDeployment, reason: ExternalBlockReason, *, now: datetime
) -> None:
    """Block a deployment and say when it is looked at again (a wait, never forever)."""

    apply_event(row, ExternalEvent.BLOCK, reason=reason, now=now)
    if _start_failures_spent(row, reason):
        park_wait(
            row,
            reason=WaitReason.RUNTIME_EFFECT_UNCONFIRMED,
            detail=(
                f"The gateway did not start with this entry {START_FAILURE_LIMIT} "
                "times; edit it or enable it to try again."
            ),
            now=now,
            operator_action=OperatorActionName.RESUME,
        )
        return
    if reason == ExternalBlockReason.START_FAILED:
        due = DISTRIBUTED_RECOVERY.next_attempt(
            row.id, _START_FAILED_ATTEMPTS, now, ongoing_intent=True
        )
        wait = WaitReason.RUNTIME_EFFECT_UNCONFIRMED
    else:
        due = schedule(ROUTE_RECOVERY, row.id, 0, Disposition.WAIT, now).next_at
        wait = WaitReason.OBSERVATION_UNAVAILABLE
    schedule_wait(row, due, now=now, reason=wait, detail=reason.value)


def plan_externals(
    session: Session,
    *,
    now: datetime,
    credentials: Collection[str] | None,
    introduce: bool,
) -> ExternalPlan:
    """Plan every external deployment and return what the next generation serves.

    ``introduce`` lets the plan add or change entries (the maintenance pass); a
    plan made for a run's publication or a withdrawal serves only what is applied,
    so a slow or failing external entry can never fail a local route.  A change
    that only removes an entry (the operator disabled or removed it, or a binding
    no longer approves its destination) applies in every plan.
    """

    bindings = read_bindings(session)
    served: list[ServedExternal] = []
    withdrawing: set[str] = set()
    rows = session.scalars(
        select(GatewayExternalDeployment).order_by(GatewayExternalDeployment.alias)
    ).all()
    for row in rows:
        stored = row.applied_spec
        applied = _spec(stored)
        state = settle(row, now=now)
        reason = (
            ExternalBlockReason.ENTRY_INVALID
            if row.applied_revision is not None and applied is None
            else None
            if applied is None
            else destination_block(applied, bindings)
        )
        if reason is not None:
            apply_event(row, ExternalEvent.WITHDRAW, reason=reason, now=now)
            state = ExternalPublicationState.WITHDRAWING
        if state == ExternalPublicationState.WITHDRAWING:
            if row.applied_revision is None:
                apply_event(row, ExternalEvent.DRAINED, now=now)  # nothing was served
                _remove_if_requested(session, row)
            else:
                withdrawing.add(row.id)
            continue
        if state == ExternalPublicationState.WITHDRAWN:
            continue
        waiting = (
            row.next_action_at is not None and now < _aware(row.next_action_at)
        ) or _parked(row)
        if state == ExternalPublicationState.BLOCKED and not waiting:
            apply_event(row, ExternalEvent.RETRY_DUE, now=now)
            schedule_wait(row, None)
            state = ExternalPublicationState.PENDING
        if introduce and state == ExternalPublicationState.PENDING:
            candidate = _introduce(row, bindings, credentials, now=now)
            if isinstance(candidate, ServedExternal):
                served.append(candidate)
                continue
        kept = _keep(row, applied)
        if isinstance(kept, ServedExternal):
            served.append(kept)
    return ExternalPlan(tuple(served), frozenset(withdrawing))


def _start_failures_spent(
    row: GatewayExternalDeployment, reason: ExternalBlockReason | str | None
) -> bool:
    """The owner's identical-exit rule: this content has failed to start often enough.

    The one place the bound is read, so the block and the parked check agree.
    """

    return (
        reason == ExternalBlockReason.START_FAILED
        and row.start_failures >= START_FAILURE_LIMIT
    )


def _parked(row: GatewayExternalDeployment) -> bool:
    """Whether the deployment gave up on its content and waits for an edit or an enable."""

    return _start_failures_spent(row, row.reason)


def _keep(
    row: GatewayExternalDeployment, applied: ExternalDeploymentSpec | None
) -> ServedExternal | ExternalBlockReason | None:
    """The entry the gateway serves now: the applied revision, if there is one."""

    if applied is None or row.applied_revision is None:
        return None
    return _serve(row, applied, row.applied_revision, introduced=False)


def _introduce(
    row: GatewayExternalDeployment,
    bindings: Mapping[str, CredentialBinding],
    credentials: Collection[str] | None,
    *,
    now: datetime,
) -> ServedExternal | None:
    """The desired entry when it may publish; otherwise the deployment blocks."""

    stored = row.desired_spec
    desired = _spec(stored)
    if desired is None:
        _block(row, ExternalBlockReason.ENTRY_INVALID, now=now)
        return None
    reason = destination_block(desired, bindings)
    if reason is None and not _loaded(desired, credentials):
        reason = ExternalBlockReason.CREDENTIAL_MISSING
    if reason is not None:
        _block(row, reason, now=now)
        return None
    served = _serve(row, desired, row.desired_revision, introduced=True)
    if isinstance(served, ExternalBlockReason):
        _block(row, served, now=now)
        return None
    return served


def _loaded(spec: ExternalDeploymentSpec, credentials: Collection[str] | None) -> bool:
    """Whether the supervisor loaded every secret the entry references."""

    return not spec.credential_bindings or (
        credentials is not None and set(spec.credential_bindings) <= set(credentials)
    )


def _remove_if_requested(session: Session, row: GatewayExternalDeployment) -> None:
    """A removed deployment leaves once its withdrawal is confirmed: its name is free."""

    if row.removal_requested:
        session.delete(row)


def record_externals(session: Session, plan: ExternalPlan, *, now: datetime) -> None:
    """Settle ``plan`` after the supervisor acknowledged its generation.

    A conditional write: a row whose desired content moved on (a newer edit) is
    left for its own next plan.
    """

    for item in plan.introduced:
        row = session.get(GatewayExternalDeployment, item.deployment_id)
        if row is None or not _still_desired(row, item):
            continue
        row.applied_revision = item.revision
        row.applied_spec = item.spec
        apply_event(row, ExternalEvent.ACKED, now=now)
        schedule_wait(row, None)
    for deployment_id in sorted(plan.withdrawing):
        row = session.get(GatewayExternalDeployment, deployment_id)
        if row is None or row.state != ExternalPublicationState.WITHDRAWING.value:
            continue
        row.applied_revision = None
        row.applied_spec = None
        apply_event(row, ExternalEvent.DRAINED, now=now)
        schedule_wait(row, None)
        _remove_if_requested(session, row)


def _still_desired(row: GatewayExternalDeployment, item: ServedExternal) -> bool:
    """Whether the row still wants exactly the content the generation served."""

    stored = row.desired_spec
    return (
        row.state == ExternalPublicationState.PENDING.value
        and _spec(stored) == item.spec
    )


def block_introduced(session: Session, plan: ExternalPlan, *, now: datetime) -> None:
    """A generation that introduced entries did not start: block exactly those entries."""

    for item in plan.introduced:
        row = session.get(GatewayExternalDeployment, item.deployment_id)
        if row is not None and _still_desired(row, item):
            row.start_failures += 1
            _block(row, ExternalBlockReason.START_FAILED, now=now)

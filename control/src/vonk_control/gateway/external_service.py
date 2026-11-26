"""Preview and accept external models and credential bindings.

An operator asks (preview), reads what the change would do, and accepts exactly
that review: the accept carries the review's ``effect_digest`` and is refused when
anything the review read has changed.  Accepting only records the operator's intent
and its identity in SQL, in one short transaction with no network call; the route
service publishes it from there (``external_publication``), so a crash after the
accept loses nothing and repeats nothing.  A request id that already holds the
deployment reconnects to the original acceptance.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_agent_protocol.outcome import InvalidRequestError
from vonk_agent_protocol.route_activation import ROUTE_SHUTDOWN_SECONDS
from vonk_agent_protocol.state_machines import (
    ExternalBlockReason,
    ExternalPublicationState,
)
from vonk_control.admission_locking import ConstraintReplan, admitting
from vonk_control.gateway.alias_namespace import (
    AliasOwner,
    AliasTaken,
    claim_external_alias,
)
from vonk_control.gateway.external_api_contract import (
    BindingReviewed,
    ExternalBindingAccepted,
    ExternalBindingAcceptRequest,
    ExternalBindingAction,
    ExternalBindingDependent,
    ExternalBindingList,
    ExternalBindingPreviewRequest,
    ExternalBindingReview,
    ExternalBindingView,
    ExternalEffect,
    ExternalModelAccepted,
    ExternalModelAcceptRequest,
    ExternalModelAction,
    ExternalModelList,
    ExternalModelPreviewRequest,
    ExternalModelReview,
    ExternalModelView,
    ExternalValidation,
    ModelReviewed,
)
from vonk_control.gateway.external_contract import (
    ApprovedDestination,
    CredentialBinding,
    ExternalDeploymentSpec,
    ExternalEntryCode,
    ExternalEntryProblem,
    ExternalModelInput,
)
from vonk_control.gateway.external_publication import CredentialNames, read_bindings
from vonk_control.gateway.external_states import ExternalEvent, apply_event
from vonk_control.gateway.external_validation import (
    destination_block,
    validate_external_entry,
)
from vonk_control.gateway.litellm_surface import LiteLlmSurface
from vonk_control.models.gateway import (
    GatewayCredentialBinding,
    GatewayExternalDeployment,
)
from vonk_control.waits.persisted import schedule_wait


class ExternalChangeConflict(InvalidRequestError):
    """What the review read has changed, or the request cannot apply: ask again."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=InvalidRequestReason.CONFLICT)


class ExternalNotFound(InvalidRequestError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"No external model or binding named {name}.",
            reason=InvalidRequestReason.NOT_FOUND,
        )


class ExternalEntryInvalid(InvalidRequestError):
    """The entry breaks an admission rule; the review lists every problem."""

    def __init__(self, problems: list[ExternalEntryProblem]) -> None:
        paths = ", ".join(sorted({problem.path for problem in problems}))[:200]
        super().__init__(
            f"The entry is refused at: {paths}.",
            reason=InvalidRequestReason.MALFORMED,
        )
        self.problems = problems


@dataclass(frozen=True)
class _ModelChange:
    review: ExternalModelReview
    spec: ExternalDeploymentSpec | None
    row: GatewayExternalDeployment | None


def _digest(document: ModelReviewed | BindingReviewed) -> str:
    return hashlib.sha256(
        document.model_dump_json(exclude_none=True).encode()
    ).hexdigest()


def _held(binding: CredentialBinding | None) -> int | None:
    """The revision a binding record holds, ``None`` when there is no record."""

    return None if binding is None else binding.revision


class ExternalModelService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        surface: Callable[[], LiteLlmSurface],
        clock: Callable[[], datetime],
        credentials: CredentialNames,
    ) -> None:
        self.sessions = sessions
        self._surface = surface
        self._clock = clock
        self._credentials = credentials

    # --- models -----------------------------------------------------------------

    def list_models(self) -> ExternalModelList:
        with self.sessions() as session:
            rows = session.scalars(
                select(GatewayExternalDeployment).order_by(
                    GatewayExternalDeployment.alias
                )
            ).all()
            return ExternalModelList(models=[_view(row) for row in rows])

    def get_model(self, alias: str) -> ExternalModelView:
        with self.sessions() as session:
            row = _row(session, alias)
            if row is None:
                raise ExternalNotFound(alias)
            return _view(row)

    def validate(self, entry: ExternalModelInput) -> ExternalValidation:
        with self.sessions() as session:
            row = _row(session, entry.model_name)
            result = self._admit(session, entry, row)
        if isinstance(result, list):
            return ExternalValidation(problems=result)
        return ExternalValidation(problems=[], destination=result.destination)

    def preview_model(
        self, alias: str, request: ExternalModelPreviewRequest
    ) -> ExternalModelReview:
        with self.sessions() as session:
            return self._change(
                session, alias, request.action, request.entry, request.expected_revision
            ).review

    def accept_model(
        self, alias: str, request: ExternalModelAcceptRequest
    ) -> ExternalModelAccepted:
        now = self._clock()
        with self.sessions.begin() as session:
            row = _row(session, alias)
            if row is not None and row.request_id == request.request_id:
                return _accepted(row, request.action)  # a lost response, asked again
            change = self._change(
                session, alias, request.action, request.entry, request.expected_revision
            )
            if change.review.effect_digest != request.effect_digest:
                raise ExternalChangeConflict(
                    "The change differs from the review that was read."
                )
            if change.review.problems:
                raise ExternalEntryInvalid(change.review.problems)
            try:
                with admitting(), session.begin_nested():
                    row = self._apply(session, change, request, now=now)
                    session.flush()
            except ConstraintReplan:
                # Two accepts took one name: the unique alias settled it.
                raise AliasTaken(alias, AliasOwner.EXTERNAL) from None
            return _accepted(row, request.action)

    def receipt(self, alias: str, request_id: str) -> ExternalModelView:
        """The deployment a request id accepted, for the operator acceptance receipt."""

        with self.sessions() as session:
            row = _row(session, alias)
            if row is None or row.request_id != request_id:
                raise ExternalNotFound(alias)
            return _view(row)

    def _admit(
        self,
        session: Session,
        entry: ExternalModelInput,
        row: GatewayExternalDeployment | None,
    ) -> ExternalDeploymentSpec | list[ExternalEntryProblem]:
        try:
            claim_external_alias(
                session, entry.model_name, own_deployment_id=row.id if row else None
            )
            taken: frozenset[str] = frozenset()
        except AliasTaken:
            taken = frozenset({entry.model_name})
        return validate_external_entry(
            entry, self._surface(), read_bindings(session), taken_aliases=taken
        )

    def _change(
        self,
        session: Session,
        alias: str,
        action: ExternalModelAction,
        entry: ExternalModelInput | None,
        expected_revision: int | None,
    ) -> _ModelChange:
        row = _row(session, alias)
        if (row.desired_revision if row else None) != expected_revision:
            raise ExternalChangeConflict(
                "The model changed since it was read; review it again."
            )
        if action != ExternalModelAction.PUT and row is None:
            raise ExternalNotFound(alias)
        problems: list[ExternalEntryProblem] = []
        spec: ExternalDeploymentSpec | None = None
        if action == ExternalModelAction.PUT:
            if entry is None or entry.model_name != alias:
                problems.append(
                    ExternalEntryProblem(
                        path="model_name", code=ExternalEntryCode.TYPE_MISMATCH
                    )
                )
            else:
                admitted = self._admit(session, entry, row)
                if isinstance(admitted, list):
                    problems = admitted
                else:
                    spec = admitted
        current = None
        if row is not None:
            stored = row.desired_spec
            current = stored if isinstance(stored, ExternalDeploymentSpec) else None
        destination = spec.destination if spec is not None else None
        bindings = spec.credential_bindings if spec is not None else []
        revisions = {
            name: binding.revision
            for name, binding in read_bindings(session).items()
            if name in bindings
        }
        review = ExternalModelReview(
            alias=alias,
            action=action,
            problems=problems,
            destination=destination,
            changed_from=(
                current.destination
                if current is not None
                and destination is not None
                and current.destination != destination
                else None
            ),
            credential_bindings=bindings,
            effects=_effects(action),
            grace_seconds=ROUTE_SHUTDOWN_SECONDS,
            expected_revision=expected_revision,
            effect_digest="0" * 64,
        )
        digest = _digest(
            ModelReviewed(
                alias=alias,
                action=action,
                spec=spec,
                expected_revision=expected_revision,
                binding_revisions=revisions,
            )
        )
        return _ModelChange(
            review.model_copy(update={"effect_digest": digest}), spec, row
        )

    def _apply(
        self,
        session: Session,
        change: _ModelChange,
        request: ExternalModelAcceptRequest,
        *,
        now: datetime,
    ) -> GatewayExternalDeployment:
        action, spec, row = request.action, change.spec, change.row
        if row is None:
            assert spec is not None  # a put without problems carries its spec
            row = GatewayExternalDeployment(
                id=str(uuid4()),
                alias=change.review.alias,
                desired_revision=1,
                desired_spec=spec,
                enabled=True,
                removal_requested=False,
                state=ExternalPublicationState.PENDING.value,
                request_id=request.request_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            return row
        row.request_id = request.request_id
        if action == ExternalModelAction.PUT:
            assert spec is not None
            row.enabled = True
            row.removal_requested = False
            stored = row.desired_spec
            if stored != spec:
                row.desired_revision += 1
                row.desired_spec = spec
                apply_event(row, ExternalEvent.REVISION_ACCEPTED, now=now)
                schedule_wait(row, None)
        elif action == ExternalModelAction.ENABLE:
            row.enabled = True
            row.removal_requested = False
            apply_event(row, ExternalEvent.ENABLE, now=now)
        else:
            row.enabled = False
            row.removal_requested = action == ExternalModelAction.REMOVE
            apply_event(row, ExternalEvent.WITHDRAW, now=now)
        row.updated_at = now
        return row

    # --- credential bindings ----------------------------------------------------

    def list_bindings(self) -> ExternalBindingList:
        loaded = self._credentials()
        with self.sessions() as session:
            return ExternalBindingList(
                bindings=[
                    _binding_view(binding, loaded)
                    for binding in sorted(
                        read_bindings(session).values(), key=lambda item: item.name
                    )
                ]
            )

    def preview_binding(
        self, name: str, request: ExternalBindingPreviewRequest
    ) -> ExternalBindingReview:
        with self.sessions() as session:
            return self._binding_review(
                session,
                name,
                request.action,
                request.destinations,
                request.expected_revision,
            )

    def accept_binding(
        self, name: str, request: ExternalBindingAcceptRequest, *, actor: str
    ) -> ExternalBindingAccepted:
        now = self._clock()
        with self.sessions.begin() as session:
            row = session.get(GatewayCredentialBinding, name)
            if row is not None and row.request_id == request.request_id:
                return ExternalBindingAccepted(
                    name=name,
                    request_id=request.request_id,
                    action=request.action,
                    revision=row.revision,
                )
            review = self._binding_review(
                session,
                name,
                request.action,
                request.destinations,
                request.expected_revision,
            )
            if review.effect_digest != request.effect_digest:
                raise ExternalChangeConflict(
                    "The change differs from the review that was read."
                )
            if request.action == ExternalBindingAction.REMOVE:
                if row is not None:
                    session.delete(row)
                return ExternalBindingAccepted(
                    name=name, request_id=request.request_id, action=request.action
                )
            destinations = request.destinations or []
            if row is None:
                row = GatewayCredentialBinding(
                    name=name,
                    revision=1,
                    destinations=destinations,
                    request_id=request.request_id,
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                if review.added or review.removed:
                    row.revision += 1
                    row.destinations = destinations
                row.request_id = request.request_id
                row.actor = actor
                row.updated_at = now
            return ExternalBindingAccepted(
                name=name,
                request_id=request.request_id,
                action=request.action,
                revision=row.revision,
            )

    def binding_receipt(self, name: str, request_id: str) -> ExternalBindingView:
        with self.sessions() as session:
            row = session.get(GatewayCredentialBinding, name)
            if row is None or row.request_id != request_id:
                raise ExternalNotFound(name)
            binding = read_bindings(session)[name]
        return _binding_view(binding, self._credentials())

    def _binding_review(
        self,
        session: Session,
        name: str,
        action: ExternalBindingAction,
        destinations: list[ApprovedDestination] | None,
        expected_revision: int | None,
    ) -> ExternalBindingReview:
        bindings = dict(read_bindings(session))
        current = bindings.get(name)
        if _held(current) != expected_revision:
            raise ExternalChangeConflict(
                "The binding changed since it was read; review it again."
            )
        if action == ExternalBindingAction.REMOVE:
            if current is None:
                raise ExternalNotFound(name)
            wanted: list[ApprovedDestination] = []
            bindings.pop(name)
        else:
            if destinations is None:
                raise ExternalEntryInvalid(
                    [
                        ExternalEntryProblem(
                            path="destinations", code=ExternalEntryCode.TYPE_MISMATCH
                        )
                    ]
                )
            wanted = sorted(destinations, key=lambda item: item.origin)
            bindings[name] = CredentialBinding(
                name=name,
                revision=(current.revision if current else 0) + 1,
                destinations=wanted,
            )
        before = current.destinations if current else []
        added = [item for item in wanted if item not in before]
        removed = [item for item in before if item not in wanted]
        dependents = _dependents(session, name, bindings)
        loaded = self._credentials()
        digest = _digest(
            BindingReviewed(
                name=name,
                action=action,
                destinations=wanted,
                expected_revision=expected_revision,
                dependents=dependents,
            )
        )
        return ExternalBindingReview(
            name=name,
            action=action,
            added=added,
            removed=removed,
            dependents=dependents,
            credential_loaded=None if loaded is None else name in loaded,
            expected_revision=expected_revision,
            effect_digest=digest,
        )


def _effects(action: ExternalModelAction) -> list[ExternalEffect]:
    if action in {ExternalModelAction.PUT, ExternalModelAction.ENABLE}:
        return [ExternalEffect.GATEWAY_RESTART, ExternalEffect.OPEN_KEY_ACCESS]
    return [ExternalEffect.GATEWAY_RESTART]


def _row(session: Session, alias: str) -> GatewayExternalDeployment | None:
    return session.scalar(select(GatewayExternalDeployment).filter_by(alias=alias))


def _dependents(
    session: Session, name: str, after: Mapping[str, CredentialBinding]
) -> list[ExternalBindingDependent]:
    """Every deployment that references ``name`` and whether the change withdraws it."""

    dependents: list[ExternalBindingDependent] = []
    for row in session.scalars(
        select(GatewayExternalDeployment).order_by(GatewayExternalDeployment.alias)
    ):
        applied = row.applied_spec
        desired = row.desired_spec
        spec = applied if isinstance(applied, ExternalDeploymentSpec) else desired
        if not isinstance(spec, ExternalDeploymentSpec):
            continue
        if name not in spec.credential_bindings:
            continue
        dependents.append(
            ExternalBindingDependent(
                alias=row.alias,
                state=ExternalPublicationState(row.state),
                withdrawn=destination_block(spec, after) is not None,
            )
        )
    return dependents


def _view(row: GatewayExternalDeployment) -> ExternalModelView:
    desired = row.desired_spec
    spec = desired if isinstance(desired, ExternalDeploymentSpec) else None
    state = ExternalPublicationState(row.state)
    return ExternalModelView(
        alias=row.alias,
        destination=spec.destination if spec is not None else None,
        credential_bindings=spec.credential_bindings if spec is not None else [],
        desired_revision=row.desired_revision,
        applied_revision=row.applied_revision,
        enabled=row.enabled,
        state=state,
        reason=ExternalBlockReason(row.reason) if row.reason else None,
        next_check_at=row.next_action_at,
        published=(
            state == ExternalPublicationState.PUBLISHED
            and row.applied_revision == row.desired_revision
        ),
    )


def _binding_view(
    binding: CredentialBinding, loaded: Collection[str] | None
) -> ExternalBindingView:
    return ExternalBindingView(
        name=binding.name,
        revision=binding.revision,
        destinations=binding.destinations,
        credential_loaded=None if loaded is None else binding.name in loaded,
    )


def _accepted(
    row: GatewayExternalDeployment, action: ExternalModelAction
) -> ExternalModelAccepted:
    return ExternalModelAccepted(
        alias=row.alias,
        request_id=row.request_id,
        action=action,
        revision=row.desired_revision,
        state=ExternalPublicationState(row.state),
    )

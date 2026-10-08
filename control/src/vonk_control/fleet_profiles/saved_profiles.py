"""Saved profiles for Fleet profiles."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_agent_protocol import InvalidRequestReason, canonical_message
from vonk_agent_protocol.agent_words import (
    ProfileDocumentState,
    ProfileInstallationPolicy,
)

from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetNodeView,
    FleetProfileApplicationView,
    FleetProfileDefinition,
    FleetProfileDefinitionView,
    FleetProfileInput,
    FleetProfileList,
    FleetProfileReadView,
    FleetProfileView,
    SavedProfileProjectionIssue,
    UnavailableFleetProfileView,
)
from ..models import AgentNode, AgentNodeProfile, FleetProfile, FleetProfileApplication
from ..stored_json import read_row_column
from .contracts import (
    FleetProfileInvalid,
)
from .projection_support import (
    _aware,
    _digest,
    _with_save_notes,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def list(self) -> FleetProfileList:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        now = _aware(self._clock())
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfile).order_by(FleetProfile.number.asc()).limit(128)
                )
            )
            return FleetProfileList(
                generated_at=now,
                profiles=[self._read_view(session, row) for row in rows],
            )

    def get(self, profile_id: str) -> FleetProfileView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            row = session.get(FleetProfile, profile_id)
            if row is None:
                raise MissingRecord(profile_id, reason=InvalidRequestReason.NOT_FOUND)
            return self._view(session, row)

    def get_number(self, number: int) -> FleetProfileView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        if type(number) is not int or number < 1:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
            if row is None:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            return self._view(session, row)

    def read_number(self, number: int) -> FleetProfileReadView:
        """Read a stable unused number without creating persistent state."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            if type(number) is not int or number < 1:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            with self._sessions() as session:
                row = session.scalar(
                    select(FleetProfile).where(FleetProfile.number == number)
                )
                if row is None:
                    raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
                return self._read_view(session, row)
        except KeyError:
            if type(number) is not int or number < 1:
                raise
            now = _aware(self._clock())
            with self._sessions() as session:
                roster = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.revoked_at.is_(None))
                        .order_by(AgentNode.node_id)
                    )
                )
                display_names = {
                    item.node_id: item.display_name
                    for item in session.scalars(
                        select(AgentNodeProfile).where(
                            AgentNodeProfile.node_id.in_(
                                [node.node_id for node in roster]
                            )
                        )
                    )
                }
            fleet = [
                FleetNodeView(
                    selector=node.node_id,
                    display_name=display_names.get(node.node_id, node.node_id),
                    state="Idle",
                )
                for node in roster
            ]
            document = {
                "schema_version": 2,
                "number": number,
                "revision": 0,
                "assignments": [],
            }
            return FleetProfileView(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:profile:{number}")),
                number=number,
                revision=0,
                name="Default" if number == 1 else f"Profile {number}",
                description="",
                installation_policy=ProfileInstallationPolicy.KEEP_CACHED.value,
                labels={},
                favorite=False,
                definition=FleetProfileDefinition(
                    name="Default" if number == 1 else f"Profile {number}"
                ),
                assignments=[],
                fleet=fleet,
                status=ProfileDocumentState.NOT_CREATED.value,
                warnings=["Profile has not been created; the first edit will save it"],
                next_actions=[
                    f"vonkctl --profile {number} profile add RECIPE --spark SPARK"
                ],
                profile_digest=_digest(document),
                created_by="uncreated",
                created_at=now,
                updated_at=now,
            )

    @staticmethod
    def _definition(row: FleetProfile) -> FleetProfileDefinition:
        """Read exact authoring intent; partial reconstruction could delete choices."""

        return FleetProfileDefinition.model_validate_json(
            canonical_message(
                {
                    name: read_row_column(row, name)
                    if name in {"labels", "assignments"}
                    else getattr(row, name)
                    for name in FleetProfileDefinition.model_fields
                }
            ),
            strict=True,
        )

    @staticmethod
    def _definition_issue() -> SavedProfileProjectionIssue:
        return SavedProfileProjectionIssue(
            detail="The saved profile definition cannot be read. Its contents are unknown.",
            next_action="Restore the saved definition or explicitly import a complete replacement at the current revision.",
        )

    def _read_view(self, session: Session, row: FleetProfile) -> FleetProfileReadView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        try:
            self._definition(row)
        except (TypeError, ValueError):
            return UnavailableFleetProfileView(
                id=row.id,
                number=row.number,
                revision=row.revision,
                projection_issue=self._definition_issue(),
            )
        return self._view(session, row)

    def definition_number(self, number: int) -> FleetProfileDefinitionView:
        """Read authoring intent without consulting catalog, cache, or runtime."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        if type(number) is not int or number < 1:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
            if row is None:
                return FleetProfileDefinitionView(
                    id=None,
                    number=number,
                    revision=0,
                    definition=FleetProfileDefinition(
                        name="Default" if number == 1 else f"Profile {number}"
                    ),
                )
            try:
                definition = self._definition(row)
            except (TypeError, ValueError):
                return FleetProfileDefinitionView(
                    id=row.id,
                    number=row.number,
                    revision=row.revision,
                    definition=None,
                    projection_issue=self._definition_issue(),
                )
            return FleetProfileDefinitionView(
                id=row.id,
                number=row.number,
                revision=row.revision,
                definition=definition,
            )

    def create(
        self, value: FleetProfileInput, *, actor: str, number: int | None = None
    ) -> FleetProfileView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            assignments, notes = self._validated_assignments(session, value.assignments)
            row = FleetProfile(
                number=number
                if number is not None
                else self._next_profile_number(session),
                revision=1,
                name=value.name,
                description=value.description,
                installation_policy=value.installation_policy,
                labels=dict(value.labels),
                favorite=value.favorite,
                assignments=[item.model_dump(mode="json") for item in assignments],
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise FleetProfileInvalid(
                    "a Fleet profile with this name already exists",
                    reason=InvalidRequestReason.DUPLICATE,
                ) from error
            result = self._view(session, row)
        return _with_save_notes(result, notes)

    def update(
        self, profile_id: str, value: FleetProfileInput, *, actor: str
    ) -> FleetProfileView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        del (
            actor
        )  # Updates retain the original creator and are audited at the API boundary.
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(FleetProfile, profile_id, with_for_update=True)
            if row is None:
                raise MissingRecord(profile_id, reason=InvalidRequestReason.NOT_FOUND)
            if row.revision != value.expected_revision:
                raise FleetProfileInvalid(
                    f"profile revision conflict: expected {value.expected_revision}, current {row.revision}",
                    reason=InvalidRequestReason.CONFLICT,
                )
            assignments, notes = self._validated_assignments(session, value.assignments)
            row.name = value.name
            row.description = value.description
            row.installation_policy = value.installation_policy
            row.labels = dict(value.labels)
            row.favorite = value.favorite
            row.revision += 1
            row.assignments = [item.model_dump(mode="json") for item in assignments]
            row.updated_at = now
            try:
                session.flush()
            except IntegrityError as error:
                raise FleetProfileInvalid(
                    "a Fleet profile with this name already exists",
                    reason=InvalidRequestReason.DUPLICATE,
                ) from error
            result = self._view(session, row)
        return _with_save_notes(result, notes)

    def update_number(
        self, number: int, value: FleetProfileInput, *, actor: str
    ) -> FleetProfileView:
        """Autosave a numbered profile with optimistic concurrency."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
        if row is None:
            if type(number) is not int or number < 1:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            if value.expected_revision != 0:
                raise FleetProfileInvalid(
                    "profile revision conflict: profile has not been created",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return self.create(value, actor=actor, number=number)
        return self.update(row.id, value, actor=actor)

    def load(
        self,
        number: int,
        *,
        actor: str,
        request_key: str,
        reviewed_effects_digest: str | None = None,
    ) -> FleetProfileApplicationView:
        """Admit the current plan; replay before consulting mutable choices.

        A caller that showed a review names its effects digest. The plan is
        then admitted only while it still has those effects.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            profile_id = session.scalar(
                select(FleetProfile.id).where(FleetProfile.number == number)
            )
        if profile_id is None:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        return self.apply(
            profile_id,
            request_key=request_key,
            actor=actor,
            reviewed_effects_digest=reviewed_effects_digest,
        )

    def progress_number(self, number: int) -> FleetProfileApplicationView:
        # Operation observation needs the stable profile identity, not today's
        # mutable saved choices. Damaged draft metadata must not hide readable
        # immutable application progress.
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            profile_id = session.scalar(
                select(FleetProfile.id).where(FleetProfile.number == number)
            )
            if profile_id is None:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            row = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.profile_id == profile_id)
                .order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(1)
            )
            if row is None:
                raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
            return self._application_view(row)

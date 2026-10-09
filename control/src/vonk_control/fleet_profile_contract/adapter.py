"""Fleet profile contract: adapter."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from .applications import FleetProfileChildOperation
from .definitions import FleetProfileAssignment
from .review import FleetProfilePreview

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..lifecycle.evidence import Residue


class FleetProfileSwitchAdapter(Protocol):
    """Profile boundary for the integrated automatic Run switch service."""

    def validate_resources_in_session(
        self,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        reviewed: FleetProfilePreview,
    ) -> None:
        """Recheck resource eligibility inside the admission writer fence."""

        ...

    def request_superseded_workload_cancellation_in_session(
        self,
        session: Session,
        targets: tuple[str, ...],
        ordinal: int,
        now: datetime,
    ) -> None:
        """Cancel older exact agent orders in the profile admission transaction."""

        ...

    def recoverable_cache_loss(self, application_id: str, *, session: Session) -> bool:
        """Whether the current exact child failed only because managed bytes vanished."""

        ...

    def recovery_refused(self, application_id: str, *, session: Session) -> bool:
        """Whether a failed child must not be replayed by profile recovery."""

        ...

    def failure_signature(self, application_id: str, *, session: Session) -> str | None:
        """The failure's identity without ids, counts or times; None if not stable."""

        ...

    def request_cancellation(
        self,
        application_id: str,
        *,
        request_key: str,
        actor: str,
    ) -> None:
        """Request cancellation from the existing Run/Switch child owner."""

        ...

    def start(
        self,
        *,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation | Residue:
        """Reconcile the complete desired assignment set as one child operation.

        ``assignments`` is ordered by stable assignment identity and
        ``scope_node_ids`` is the complete sorted profile boundary.  The
        implementation must plan conflicts once and preserve healthy desired
        assignments while preparing or stopping other members.  Evidence it
        cannot establish (a damaged stored plan or intent) is returned as a
        ``Residue``: the application retires, it is not refused.
        """

        ...

    def get(
        self, operation_id: str, *, session: Session | None = None
    ) -> FleetProfileChildOperation:
        """Observe the durable child without ticking or dispatching work."""

        ...

    def advance(
        self, operation_id: str, *, session: Session | None = None
    ) -> FleetProfileChildOperation:
        """Advance the durable child from the worker's execution path.

        A caller that already holds this application's row passes its session so
        the adapter joins that transaction instead of opening a second one on a
        row the caller has locked.
        """

        ...

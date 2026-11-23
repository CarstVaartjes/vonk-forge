"""Distribution: service."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy.orm import Session, sessionmaker

from vonk_agent_protocol.distribution import DistributionObject
from vonk_control.distribution.delivery import DeliveryMixin
from vonk_control.distribution.locations import ObjectLocation
from vonk_control.distribution.registration import RegistrationMixin
from vonk_control.distribution.types import ObjectSource
from vonk_control.distribution_assignment import NodeDistributionAssignment


class DistributionService(RegistrationMixin, DeliveryMixin):
    """Resolves exact assignments and serves only their declared objects."""

    def __init__(
        self,
        source: ObjectSource,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sessions: sessionmaker[Session] | None = None,
    ) -> None:
        self.source = source
        self.clock = clock
        self.sessions = sessions
        # In-memory mode (no database) is a test double. Its dict needs a lock
        # for check-then-write in register/revoke only; database mode takes no
        # process-wide lock because Postgres arbitrates concurrent access.
        self._assignments: dict[tuple[str, str], NodeDistributionAssignment] = {}
        self._lock = Lock()
        # Authorization is decided per assignment, not per range request.
        # Bounded and short-lived so a revocation made by another process is
        # seen within _AUTHORIZATION_TTL_SECONDS (eventually consistent).
        self._authorized: OrderedDict[
            tuple[str, str], tuple[NodeDistributionAssignment, float]
        ] = OrderedDict()
        self._authorized_lock = Lock()  # guards only the dicts, never I/O
        # Where an authorized object sits, remembered like the authorization:
        # a transfer asks once per 64 MiB range, and resolving the object
        # reads the cache manifest each time. Objects are immutable
        # and content addressed, and every hit still checks the file's type and
        # size, so only a deletion within the window waits for it to end.
        self._located: OrderedDict[
            tuple[str, str, str],
            tuple[DistributionObject, ObjectLocation, float],
        ] = OrderedDict()

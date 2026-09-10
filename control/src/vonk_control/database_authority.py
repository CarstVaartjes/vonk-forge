"""PostgreSQL-backed control authority head and revision persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ControlAuthorityHead,
    ControlAuthorityRevision,
)


class AuthorityPolicyError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _revision(documents: Mapping[str, object], dependencies: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical({"dependencies": dependencies, "documents": documents})
    ).hexdigest()


class DatabaseAuthorityService:
    """Persist the immutable authority revision and PostgreSQL-owned head."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def ensure_initialized(self, *, acquire_advisory_lock: bool = True) -> str:
        with self._sessions.begin() as session:
            if (
                acquire_advisory_lock
                and session.bind is not None
                and session.bind.dialect.name == "postgresql"
            ):
                # Serialize first-use initialization without relying on a row that
                # does not exist yet. This keeps concurrent API replicas safe.
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": 8_241_779_103},
                )
            head = session.get(ControlAuthorityHead, 1)
            if head is not None:
                return head.revision_id
            dependencies: dict[str, list[str]] = {}
            documents: dict[str, object] = {}
            revision_id = _revision(documents, dependencies)
            now = self._clock()
            session.add(
                ControlAuthorityRevision(
                    revision_id=revision_id,
                    parent_revision=None,
                    documents=documents,
                    dependencies=dependencies,
                    actor="system/bootstrap",
                    created_at=now,
                )
            )
            session.flush()
            session.add(
                ControlAuthorityHead(
                    singleton_id=1,
                    revision_id=revision_id,
                    updated_at=now,
                )
            )
            return revision_id

    def head(self) -> str:
        self.ensure_initialized()
        with self._sessions() as session:
            head = session.get(ControlAuthorityHead, 1)
            if head is None:
                raise AuthorityPolicyError("authority head is unavailable")
            return head.revision_id

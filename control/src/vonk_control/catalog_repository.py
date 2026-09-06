"""Canonical document persistence helpers and source redaction rules."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import CatalogDocument, CatalogDocumentRevision

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:authorization|credential|password|secret|token|private_key|certificate)(?:$|_)",
    re.IGNORECASE,
)


class CatalogRepository:
    """Keep canonical document lookup and revision allocation in one boundary."""

    def document(
        self, session: Session, document_id: str, *, for_update: bool = False
    ) -> CatalogDocument | None:
        statement = select(CatalogDocument).where(CatalogDocument.id == document_id)
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def document_by_identity(
        self,
        session: Session,
        kind: str,
        publisher: str,
        slug: str,
        *,
        for_update: bool = False,
    ) -> CatalogDocument | None:
        statement = select(CatalogDocument).where(
            CatalogDocument.kind == kind,
            CatalogDocument.publisher == publisher,
            CatalogDocument.slug == slug,
        )
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def active_revision(
        self, session: Session, document_id: str
    ) -> CatalogDocumentRevision | None:
        return session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.document_id == document_id,
                CatalogDocumentRevision.state == "active",
            )
        )

    def latest_revision(
        self, session: Session, document_id: str
    ) -> CatalogDocumentRevision | None:
        return session.scalar(
            select(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.document_id == document_id)
            .order_by(CatalogDocumentRevision.revision_number.desc())
            .limit(1)
        )

    def revision(
        self, session: Session, document_id: str, revision_number: int
    ) -> CatalogDocumentRevision | None:
        return session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.document_id == document_id,
                CatalogDocumentRevision.revision_number == revision_number,
            )
        )

    def next_revision_number(self, session: Session, document_id: str) -> int:
        if self.document(session, document_id, for_update=True) is None:
            raise KeyError(document_id)
        current = session.scalar(
            select(func.max(CatalogDocumentRevision.revision_number)).where(
                CatalogDocumentRevision.document_id == document_id
            )
        )
        return int(current or 0) + 1

    def redact_source(self, source: object) -> object:
        def redact(value: object) -> object:
            if isinstance(value, Mapping):
                return {
                    str(key): "[REDACTED]"
                    if _SENSITIVE_KEY.search(str(key))
                    else redact(child)
                    for key, child in value.items()
                }
            if isinstance(value, list):
                return [redact(child) for child in value]
            return copy.deepcopy(value)

        return redact(source)


def sensitive_document_path(document: object) -> str | None:
    """Return only the sensitive key path; values never enter errors or logs."""

    def inspect(value: object, path: str) -> str | None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}"
                if _SENSITIVE_KEY.search(key_text):
                    return child_path
                found = inspect(child, child_path)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found = inspect(child, f"{path}[{index}]")
                if found is not None:
                    return found
        return None

    return inspect(document, "$")

"""PostgreSQL-backed control authority and durable proposal workflow."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    ControlAuthorityHead,
    ControlAuthorityProposal,
    ControlAuthorityRevision,
)
from .serializers import serialize_document

_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_ROOTS = ("inventory/", "locks/", "manifests/", "docs/audits/")
_DEFAULT_DOCUMENTS: dict[str, object] = {
    "inventory/topology.json": {
        "schema_version": 1,
        "nodes": [],
        "links": [],
    }
}


class AuthorityPolicyError(ValueError):
    pass


class StaleAuthorityRevision(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorityDocument:
    revision: str
    path: str
    content: bytes
    sha256: str
    parsed: object


@dataclass(frozen=True)
class AuthoritySnapshot:
    revision: str
    documents: Mapping[str, str]
    dependencies: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class AuthorityChange:
    path: str
    document: Mapping[str, object]


@dataclass(frozen=True)
class AuthorityProposalPreview:
    actor: str
    base_revision: str
    patch: bytes
    affected_documents: tuple[str, ...]
    validation_results: tuple[str, ...]
    digest: str


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


def _document_map(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AuthorityPolicyError("authority documents are invalid")
    return {str(path): document for path, document in value.items()}


class DatabaseAuthorityService:
    """Immutable authority revisions with a PostgreSQL-owned current head."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def ensure_initialized(self) -> str:
        with self._sessions.begin() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
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
            documents = dict(_DEFAULT_DOCUMENTS)
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
            session.add(
                ControlAuthorityHead(
                    singleton_id=1,
                    revision_id=revision_id,
                    updated_at=now,
                )
            )
            return revision_id

    def head(self, _branch: str = "HEAD") -> str:
        self.ensure_initialized()
        with self._sessions() as session:
            head = session.get(ControlAuthorityHead, 1)
            if head is None:
                raise AuthorityPolicyError("authority head is unavailable")
            return head.revision_id

    def _revision_row(self, session: Session, revision: str | None) -> ControlAuthorityRevision:
        selected = revision or self.head()
        if _REVISION.fullmatch(selected) is None:
            raise AuthorityPolicyError("authority revision is invalid")
        row = session.get(ControlAuthorityRevision, selected)
        if row is None:
            raise AuthorityPolicyError("authority revision is unavailable")
        return row

    def inspect(self, revision: str | None = None) -> AuthoritySnapshot:
        with self._sessions() as session:
            row = self._revision_row(session, revision)
            documents = {
                path: base64.b64encode(serialize_document(path, document)).decode("ascii")
                for path, document in _document_map(row.documents).items()
            }
            dependencies = {
                path: tuple(values)
                for path, values in _document_map(row.dependencies).items()
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
            }
            return AuthoritySnapshot(
                row.revision_id,
                MappingProxyType(documents),
                MappingProxyType(dependencies),
            )

    def read_document(self, revision: str, path: str) -> AuthorityDocument:
        self.validate_path(path)
        with self._sessions() as session:
            row = self._revision_row(session, revision)
            documents = _document_map(row.documents)
            try:
                parsed = documents[path]
            except KeyError:
                raise AuthorityPolicyError("managed document does not exist") from None
            content = serialize_document(path, parsed)
            return AuthorityDocument(
                row.revision_id,
                path,
                content,
                hashlib.sha256(content).hexdigest(),
                parsed,
            )

    @staticmethod
    def validate_path(path: str) -> str:
        if (
            not isinstance(path, str)
            or "\\" in path
            or "\x00" in path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not any(path.startswith(root) for root in _ALLOWED_ROOTS)
        ):
            raise AuthorityPolicyError("managed document path is not allowlisted")
        return path

    def _snapshot_row(self, session: Session, revision: str) -> ControlAuthorityRevision:
        row = self._revision_row(session, revision)
        return row

    def apply(self, preview: AuthorityProposalPreview) -> str:
        with self._sessions.begin() as session:
            proposal = session.scalar(
                select(ControlAuthorityProposal)
                .where(ControlAuthorityProposal.digest == preview.digest)
                .with_for_update()
            )
            if proposal is None:
                raise AuthorityPolicyError("authority proposal is unavailable")
            if proposal.applied_revision is not None:
                return proposal.applied_revision
            head = session.scalar(
                select(ControlAuthorityHead).where(
                    ControlAuthorityHead.singleton_id == 1
                ).with_for_update()
            )
            if head is None or head.revision_id != proposal.base_revision:
                raise StaleAuthorityRevision(
                    "proposal base revision is no longer authority head"
                )
            parent = self._snapshot_row(session, proposal.base_revision)
            documents = _document_map(parent.documents)
            for change in proposal.changes:
                documents[str(change["path"])] = change["document"]
            dependencies = _document_map(parent.dependencies)
            revision_id = _revision(documents, dependencies)
            existing = session.get(ControlAuthorityRevision, revision_id)
            if existing is None:
                session.add(
                    ControlAuthorityRevision(
                        revision_id=revision_id,
                        parent_revision=proposal.base_revision,
                        documents=documents,
                        dependencies=dependencies,
                        actor=proposal.actor,
                        created_at=self._clock(),
                    )
                )
            head.revision_id = revision_id
            head.updated_at = self._clock()
            proposal.applied_revision = revision_id
            return revision_id


class DatabaseProposalService:
    def __init__(self, authority: DatabaseAuthorityService) -> None:
        self._authority = authority

    def head(self) -> str:
        return self._authority.head()

    def preview(
        self,
        actor: str,
        base_revision: str,
        changes: Sequence[AuthorityChange],
    ) -> AuthorityProposalPreview:
        if not actor.strip() or not changes:
            raise ValueError("proposal actor and changes are required")
        self._authority.inspect(base_revision)
        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        for change in changes:
            path = self._authority.validate_path(change.path)
            if path in seen:
                raise ValueError(f"duplicate proposal path: {path}")
            seen.add(path)
            if not isinstance(change.document, Mapping):
                raise ValueError("proposal document must be an object")
            serialize_document(path, change.document)
            normalized.append({"path": path, "document": dict(change.document)})
        normalized.sort(key=lambda value: str(value["path"]))
        patch = _canonical({"base_revision": base_revision, "changes": normalized})
        digest = hashlib.sha256(patch).hexdigest()
        now = self._authority._clock()
        with self._authority._sessions.begin() as session:
            existing = session.get(ControlAuthorityProposal, digest)
            if existing is None:
                session.add(
                    ControlAuthorityProposal(
                        digest=digest,
                        actor=actor,
                        base_revision=base_revision,
                        changes=normalized,
                        patch=patch,
                        affected_documents=[str(value["path"]) for value in normalized],
                        validation_results=["typed-syntax:passed", "path-policy:passed"],
                        created_at=now,
                    )
                )
        return AuthorityProposalPreview(
            actor,
            base_revision,
            patch,
            tuple(str(value["path"]) for value in normalized),
            ("typed-syntax:passed", "path-policy:passed"),
            digest,
        )

    def apply(self, digest: str) -> AuthorityProposalPreview:
        with self._authority._sessions() as session:
            row = session.get(ControlAuthorityProposal, digest)
            if row is None:
                raise ValueError("unknown proposal digest")
            return AuthorityProposalPreview(
                row.actor,
                row.base_revision,
                row.patch,
                tuple(row.affected_documents),
                tuple(row.validation_results),
                row.digest,
            )


class DatabaseChangeService:
    def __init__(self, authority: DatabaseAuthorityService, proposals: DatabaseProposalService) -> None:
        self._authority = authority
        self._proposals = proposals

    def submit(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        preview = self._proposals.apply(digest)
        revision = self._authority.apply(preview)
        return {
            "proposal_digest": digest,
            "previous_revision": preview.base_revision,
            "authority_revision": revision,
            "mode": "database",
        }

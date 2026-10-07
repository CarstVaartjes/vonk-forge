"""Updates."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ModelCacheCode
from vonk_forge_contracts import ModelDefinition
from vonk_forge_contracts.model import ModelReference

from ..catalog_queries import active_head_revision
from ..catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
)
from ..categorized_errors import InvalidValue
from ..model_cache_contract import ModelCacheUpstreamRevision
from ..models import CatalogDocumentRevision, ModelCacheSet
from ..strict_json import serialize_json_value
from .artifacts import ArtifactSetManifest, _optional_digest
from .catalog_helpers import (
    _datetime,
    _iso,
    _model_lineage_signature,
    _parse_iso,
    _revision_identity,
    _same_model_artifact_identity,
)
from .constants import (
    _UPSTREAM_CHECK_SECONDS,
    _UPSTREAM_CHECK_WORKERS,
    SCHEMA_VERSION,
    SOURCE_POLICY,
)
from .errors import ModelCacheConflictInvalid, ModelCacheError

if TYPE_CHECKING:
    from .service import ModelCacheService


class UpdatesMixin:
    """Updates behavior of the cache service."""

    @staticmethod
    def _model_update_candidate(
        session: Session, manifest: ArtifactSetManifest
    ) -> tuple[CatalogDocumentRevision, CatalogDocumentRevision] | None:
        from .service import ModelCacheService

        current, candidates = ModelCacheService._model_update_candidates(
            session, manifest
        )
        if current is None or len(candidates) != 1:
            return None
        return current, candidates[0]

    @staticmethod
    def _model_update_candidates(
        session: Session, manifest: ArtifactSetManifest
    ) -> tuple[CatalogDocumentRevision | None, list[CatalogDocumentRevision]]:
        ref = manifest.model_definition_ref
        if ref is None:
            return None, []
        current_digest = ref.content_sha256
        current = None
        current = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.content_digest == current_digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if current is None:
            current = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.kind == "model",
                    CatalogDocumentRevision.publisher == ref.publisher,
                    CatalogDocumentRevision.slug == ref.slug,
                    CatalogDocumentRevision.state == "active",
                )
                .order_by(CatalogDocumentRevision.revision_number.asc())
            )
        if current is None:
            return None, []
        try:
            current_document = read_catalog_document(current)
        except CatalogRevisionContractError:
            return None, []
        if not isinstance(current_document, ModelDefinition):
            return None, []
        current_signature = _model_lineage_signature(current_document)
        candidates: list[CatalogDocumentRevision] = []
        for candidate in session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
        ):
            if candidate.content_digest == current.content_digest:
                continue
            try:
                candidate_document = read_catalog_document(candidate)
            except CatalogRevisionContractError:
                continue
            if not isinstance(candidate_document, ModelDefinition):
                continue
            if _model_lineage_signature(candidate_document) != current_signature:
                continue
            if not _same_model_artifact_identity(candidate, manifest) and (
                candidate.revision_number > current.revision_number
                or _datetime(candidate.created_at) > _datetime(current.created_at)
            ):
                candidates.append(candidate)
        # Multiple incomparable successors are deliberately exposed as
        # ambiguous; choosing one by wall-clock order would hide a catalog
        # lineage decision from operators.
        return current, candidates

    @staticmethod
    def _latest_recipe_digest(session: Session, digest: str) -> str | None:
        current = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.content_digest == digest,
                CatalogDocumentRevision.state == "active",
            )
        )
        if current is None:
            return None
        latest = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.document_id == current.document_id,
                CatalogDocumentRevision.state == "active",
                active_head_revision(),
            )
        )
        return None if latest is None else latest.content_digest

    def _check_upstream_revision(
        self, repository: str, revision: str
    ) -> ModelCacheUpstreamRevision:
        """Inspect provider metadata only; catalog import owns accepting new pins."""
        cache = cast("ModelCacheService", self)
        result = ModelCacheUpstreamRevision(
            repository=repository,
            pinned_revision=revision,
            status="check-failed",
            checked_at=_iso(cache._clock()) or "",
        )
        own_client = cache._http is None
        client = cache._http or httpx2.Client(timeout=20, follow_redirects=False)
        try:
            response = cache._open_http_response(
                client,
                f"https://huggingface.co/api/models/{repository}/revision/main",
                {},
            )
            try:
                response.read()
                document = response.json()
            finally:
                response.close()
            latest = document.get("sha") if isinstance(document, dict) else None
            if isinstance(latest, str) and re.fullmatch(r"[0-9a-f]{40,64}", latest):
                result = result.model_copy(
                    update={
                        "latest_revision": latest,
                        "status": "current"
                        if latest == revision
                        else "update-available",
                    }
                )
            else:
                # Not an immutable revision: reported as an unknown check, never
                # raised (the status stays ``check-failed``).
                result.error_code = ModelCacheCode.UPSTREAM_REVISION_INVALID
        except (ModelCacheError, httpx2.HTTPError, ValueError, OSError) as error:
            # Provider failures must not hide accepted catalog updates or
            # expose signed URLs/credentials in the public response.
            result.error_code = getattr(
                error, "code", ModelCacheCode.UPSTREAM_CHECK_FAILED
            )
        finally:
            if own_client:
                client.close()
        return result

    def _check_upstream_revisions(
        self, identities: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], ModelCacheUpstreamRevision]:
        """Bound metadata concurrency and total response latency across the page."""
        cache = cast("ModelCacheService", self)
        ordered = list(dict.fromkeys(identities))
        results: dict[tuple[str, str], ModelCacheUpstreamRevision] = {}
        pending: dict[Future[ModelCacheUpstreamRevision], tuple[str, str]] = {}
        deadline = time.monotonic() + _UPSTREAM_CHECK_SECONDS
        remaining = iter(ordered)
        exhausted = False
        while time.monotonic() < deadline:
            while not exhausted and len(pending) < _UPSTREAM_CHECK_WORKERS:
                if not cache._upstream_slots.acquire(blocking=False):
                    break
                key = next(remaining, None)
                if key is None:
                    cache._upstream_slots.release()
                    exhausted = True
                    break
                try:
                    future = cache._upstream_executor.submit(
                        cache._check_upstream_revision, *key
                    )
                except RuntimeError:
                    cache._upstream_slots.release()
                    break
                future.add_done_callback(lambda _: cache._upstream_slots.release())
                pending[future] = key
            if not pending:
                break
            done, _ = wait(
                pending,
                timeout=max(0, deadline - time.monotonic()),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                key = pending.pop(future)
                try:
                    results[key] = future.result()
                except Exception:  # noqa: BLE001 - diagnostics must not hide local catalog results
                    # A diagnostic/provider bug cannot hide the catalog page.
                    results[key] = ModelCacheUpstreamRevision(
                        repository=key[0],
                        pinned_revision=key[1],
                        status="check-failed",
                        checked_at=_iso(cache._clock()) or "",
                        error_code=ModelCacheCode.UPSTREAM_CHECK_FAILED,
                    )
        for future in pending:
            future.cancel()
        for repository, revision in ordered:
            results.setdefault(
                (repository, revision),
                ModelCacheUpstreamRevision(
                    repository=repository,
                    pinned_revision=revision,
                    status="check-failed",
                    checked_at=_iso(cache._clock()) or "",
                    error_code=ModelCacheCode.UPSTREAM_CHECK_BUDGET_EXHAUSTED,
                ),
            )
        return results

    def discover_updates(
        self,
        *,
        artifact_set_sha256: str | None = None,
        limit: int = 100,
        check_upstream: bool = False,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return a bounded, deterministic update page.

        Update discovery is metadata only: it never changes the immutable
        model pin or the active profile/run reference.  The optional exact
        set filter is used by the CLI and keeps a large NAS inventory from
        becoming an unbounded response.
        """
        cache = cast("ModelCacheService", self)
        if not 1 <= limit <= 100:
            raise InvalidValue("cache update limit is invalid")
        requested_set = _optional_digest(artifact_set_sha256)
        cache.reconcile_storage()
        with cache._session() as session:
            query = select(ModelCacheSet).order_by(
                ModelCacheSet.updated_at.desc(),
                ModelCacheSet.artifact_set_sha256.desc(),
            )
            if requested_set is not None:
                query = query.where(ModelCacheSet.artifact_set_sha256 == requested_set)
            rows = list(session.scalars(query))
            total = len(rows)
            start = 0
            if boundary is not None:
                boundary_time = _parse_iso(boundary[0])
                for index, row in enumerate(rows):
                    if (
                        _datetime(row.updated_at) == boundary_time
                        and row.artifact_set_sha256 == boundary[1]
                    ):
                        start = index + 1
                        break
                else:
                    raise ModelCacheConflictInvalid(
                        ModelCacheCode.CURSOR_INVALID,
                        "cache update cursor boundary is stale",
                    )
            page = rows[start : start + limit]
            result = []
            upstream_sources: dict[str, list[tuple[str, str]]] = {}
            for row in page:
                manifest = cache._stored_manifest(row)
                if manifest is None:
                    continue  # unknown set: not listed until it is re-derived
                model_update, recipe_update = cache._update_flags(
                    session, row, manifest
                )
                latest_model = None
                model_update_from = None
                model_update_to = None
                model_update_candidates: list[ModelReference] = []
                model_update_ambiguous = False
                latest_recipe = None
                current_model, candidates = cache._model_update_candidates(
                    session, manifest
                )
                if current_model is not None and len(candidates) == 1:
                    latest = candidates[0]
                    latest_model = latest.content_digest
                    model_update_from = _revision_identity(current_model)
                    model_update_to = _revision_identity(latest)
                elif current_model is not None and candidates:
                    model_update_ambiguous = True
                    model_update_candidates = [
                        identity
                        for candidate in candidates
                        if (identity := _revision_identity(candidate)) is not None
                    ]
                if recipe_update and row.recipe_revision_sha256 is not None:
                    latest_recipe = cache._latest_recipe_digest(
                        session, row.recipe_revision_sha256
                    )
                sources: list[tuple[str, str]] = []
                if check_upstream:
                    for spec in manifest.artifacts:
                        if spec.kind != "huggingface.file" or spec.revision is None:
                            continue
                        repository = "/".join(
                            urlsplit(spec.source).path.strip("/").split("/")[:2]
                        )
                        key = (repository, spec.revision)
                        if key not in sources:
                            sources.append(key)
                upstream_sources[row.artifact_set_sha256] = sources
                result.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "artifact_set_sha256": row.artifact_set_sha256,
                        "model_content_sha256": row.model_content_sha256,
                        "latest_model_content_sha256": latest_model,
                        "model_update_from": None
                        if model_update_from is None
                        else serialize_json_value(model_update_from),
                        "model_update_to": None
                        if model_update_to is None
                        else serialize_json_value(model_update_to),
                        "model_update_ambiguous": model_update_ambiguous,
                        "model_update_candidates": [
                            serialize_json_value(item)
                            for item in model_update_candidates
                        ],
                        "recipe_revision_sha256": row.recipe_revision_sha256,
                        "latest_recipe_revision_sha256": latest_recipe,
                        "upstream_revisions": [],
                        "model_update_available": model_update,
                        "recipe_update_available": recipe_update,
                        "updated_at": _iso(row.updated_at),
                    }
                )
            next_boundary = None
            if start + limit < total and page:
                last = page[-1]
                next_boundary = (_iso(last.updated_at) or "", last.artifact_set_sha256)
        # All catalog values above are detached JSON snapshots. Provider I/O
        # must not retain a DB connection or transaction while waiting.
        if check_upstream:
            upstream_checks = cache._check_upstream_revisions(
                [key for sources in upstream_sources.values() for key in sources]
            )
            for entry in result:
                entry["upstream_revisions"] = [
                    serialize_json_value(upstream_checks[key])
                    for key in upstream_sources[entry["artifact_set_sha256"]]
                ]
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "updates": tuple(result),
            "total": total,
            "_next_boundary": next_boundary,
        }

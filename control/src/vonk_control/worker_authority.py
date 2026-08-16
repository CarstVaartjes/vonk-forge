"""Authenticated internal repository authority for the repository-less worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .litellm import LiteLlmDeployment, LiteLlmPublisher
from .route_runtime import PublishedRoute, published_routes_digest

_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_TOKEN = re.compile(rb"[A-Za-z0-9_-]{32,}\Z")
_MAX_RESPONSE = 65_536
_MAX_ATTESTATION_SECONDS = 15


class WorkerAuthorityError(RuntimeError):
    """The internal authority was unavailable or returned unsafe data."""


class DeploymentPolicy(Protocol):
    def __call__(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> tuple[LiteLlmDeployment, ...]: ...


class ReconciliationInput(Protocol):
    def __call__(
        self,
        reconciliation_id: str,
    ) -> tuple[str, str, tuple[PublishedRoute, ...], str]: ...


class AuthorityRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=128)
    workload_id: str = Field(min_length=1, max_length=128)
    api_base: str = Field(min_length=1, max_length=512)
    requests_per_minute: int = Field(ge=1, le=100_000)
    tokens_per_minute: int = Field(ge=1, le=100_000_000)


class AuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    reconciliation_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{32,64}$")
    routes: list[AuthorityRoute] = Field(max_length=64)


class UpdateGrantRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    rollout_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    batch_index: int = Field(ge=0)
    node_ids: list[str] = Field(min_length=1, max_length=1024)
    nonce: str = Field(pattern=r"^[0-9a-f]{32,64}$")


class UpdateGrantRefresher(Protocol):
    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
        *,
        actor: str,
        request_id: str,
    ) -> dict[str, object]: ...


class RepositoryAuthorityService:
    """Evaluate live Git authority and repository policy inside the API."""

    def __init__(
        self,
        *,
        current_commit: Callable[[], str],
        commit_eligible: Callable[[str], bool],
        reconciliation_input: ReconciliationInput,
        current_fleet_evidence: Callable[[], str],
        deployments: DeploymentPolicy | None = None,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._current_commit = current_commit
        self._commit_eligible = commit_eligible
        self._reconciliation_input = reconciliation_input
        self._current_fleet_evidence = current_fleet_evidence
        self._deployments = deployments
        self._clock = clock

    def current(self) -> str:
        commit = self._current_commit()
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise WorkerAuthorityError("repository head is invalid")
        return commit

    def evaluate(
        self,
        reconciliation_id: str,
        commit: str,
        plan_digest: str,
        routes: tuple[PublishedRoute, ...],
    ) -> Mapping[str, object]:
        if _COMMIT.fullmatch(commit) is None:
            raise WorkerAuthorityError("repository commit is invalid")
        (
            expected_commit,
            expected_plan_digest,
            expected_routes,
            expected_fleet_evidence,
        ) = (
            self._reconciliation_input(reconciliation_id)
        )
        if (
            not secrets.compare_digest(expected_commit, commit)
            or not secrets.compare_digest(expected_plan_digest, plan_digest)
            or expected_routes != routes
            or not isinstance(expected_fleet_evidence, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_fleet_evidence) is None
        ):
            raise WorkerAuthorityError("reconciliation authority input is invalid")
        fleet_evidence_current = secrets.compare_digest(
            self._current_fleet_evidence(), expected_fleet_evidence
        )
        current = secrets.compare_digest(self.current(), commit)
        eligible = current and self._commit_eligible(commit) is True
        deployments: tuple[LiteLlmDeployment, ...] = ()
        if (
            self._deployments is not None
            and current
            and eligible
            and fleet_evidence_current
            and routes
        ):
            deployments = self._deployments(commit, routes)
            for deployment in deployments:
                if not isinstance(deployment, LiteLlmDeployment):
                    raise WorkerAuthorityError("repository deployment is invalid")
                LiteLlmPublisher._validate_hermes_deployment(deployment)
        current = current and secrets.compare_digest(self.current(), commit)
        fleet_evidence_current = fleet_evidence_current and secrets.compare_digest(
            self._current_fleet_evidence(), expected_fleet_evidence
        )
        eligible = eligible and current
        if not eligible or not fleet_evidence_current:
            deployments = ()
        return {
            "schema_version": 1,
            "reconciliation_id": reconciliation_id,
            "commit": commit,
            "plan_digest": plan_digest,
            "current": current,
            "eligible": eligible,
            "fleet_evidence_current": fleet_evidence_current,
            "routes_sha256": published_routes_digest(routes),
            "deployments": [asdict(item) for item in deployments],
        }

    def issued_at(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerAuthorityError("repository authority clock is invalid")
        return value


def worker_document_signature(
    token: bytes,
    document: Mapping[str, object],
    *,
    purpose: str,
) -> str:
    """Return the HMAC for one canonical internal authority document."""

    if _TOKEN.fullmatch(token) is None:
        raise ValueError("worker authority token is invalid")
    if purpose not in {"request", "response"}:
        raise ValueError("worker authority signature purpose is invalid")
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    domain = f"vonk-forge-worker-authority/v1/{purpose}\0".encode()
    return hmac.new(token, domain + encoded, hashlib.sha256).hexdigest()


def install_worker_authority_routes(
    app: FastAPI,
    service: RepositoryAuthorityService,
    *,
    token: bytes,
    update_grants: UpdateGrantRefresher | None = None,
) -> None:
    """Install worker-only routes guarded by an independent service token."""

    if _TOKEN.fullmatch(token) is None:
        raise ValueError("worker authority token is invalid")
    def authenticate(request: Request, document: Mapping[str, object]) -> None:
        supplied = request.headers.get("x-vonk-worker-signature", "")
        expected = worker_document_signature(token, document, purpose="request")
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="authentication required")

    @app.post("/internal/v1/repository/evaluate", include_in_schema=False)
    def repository_evaluate(
        body: AuthorityRequest,
        request: Request,
    ) -> Mapping[str, object]:
        request_document = body.model_dump()
        authenticate(request, request_document)
        routes = tuple(PublishedRoute(**route.model_dump()) for route in body.routes)
        try:
            issued_at = service.issued_at()
            response = {
                **service.evaluate(
                    body.reconciliation_id,
                    body.commit,
                    body.plan_digest,
                    routes,
                ),
                "nonce": body.nonce,
                "issued_at": issued_at,
                "expires_at": issued_at + _MAX_ATTESTATION_SECONDS,
            }
            return {
                **response,
                "signature": worker_document_signature(
                    token,
                    response,
                    purpose="response",
                ),
            }
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="repository authority unavailable") from None

    if update_grants is not None:

        @app.post("/internal/v1/updates/grant", include_in_schema=False)
        def update_grant_refresh(
            body: UpdateGrantRefreshRequest,
            request: Request,
        ) -> Mapping[str, object]:
            request_document = body.model_dump()
            authenticate(request, request_document)
            request_id = getattr(request.state, "request_id", None)
            if request_id is None:
                request_id = request.headers.get("x-request-id", "")
            try:
                grant = update_grants.refresh_update_grant(
                    body.rollout_id,
                    body.batch_index,
                    tuple(body.node_ids),
                    actor="control-worker",
                    request_id=request_id,
                )
                if not isinstance(grant, dict):
                    raise TypeError("update grant is invalid")
                response = {**request_document, "grant": grant}
                return {
                    **response,
                    "signature": worker_document_signature(
                        token,
                        response,
                        purpose="response",
                    ),
                }
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                raise HTTPException(
                    status_code=503,
                    detail="update grant authority unavailable",
                ) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _open_without_redirect(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect,
    ).open(request, timeout=timeout)


class HttpWorkerAuthority:
    """Bounded fail-closed client used by the production worker."""

    def __init__(
        self,
        origin: str,
        token: bytes,
        *,
        timeout_seconds: float = 3.0,
        opener: Callable[..., object] = _open_without_redirect,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        if not origin or origin.endswith("/") or _TOKEN.fullmatch(token) is None:
            raise ValueError("worker authority client configuration is invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("worker authority timeout is invalid")
        self._origin = origin
        self._token = token
        self._timeout = timeout_seconds
        self._opener = opener
        self._clock = clock
        self._cached: _CachedAuthority | None = None

    def _request(
        self,
        path: str,
        *,
        document: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        content = None
        method = "GET"
        headers = {"Accept": "application/json"}
        if document is not None:
            content = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            headers["Content-Type"] = "application/json"
            headers["X-Vonk-Worker-Signature"] = worker_document_signature(
                self._token,
                document,
                purpose="request",
            )
            method = "POST"
        request = urllib.request.Request(
            self._origin + path,
            data=content,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener(request, timeout=self._timeout)
            with response:
                status = getattr(response, "status", None)
                final_url = getattr(response, "geturl", lambda: request.full_url)()
                raw = response.read(_MAX_RESPONSE + 1)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise WorkerAuthorityError("worker authority is unavailable") from error
        if (
            status != 200
            or final_url != request.full_url
            or len(raw) > _MAX_RESPONSE
        ):
            raise WorkerAuthorityError("worker authority rejected the request")
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise WorkerAuthorityError("worker authority response is invalid") from error
        if not isinstance(parsed, Mapping):
            raise WorkerAuthorityError("worker authority response is invalid")
        return parsed

    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """Obtain a fresh API-issued grant bound to one persisted rollout batch."""

        nonce = secrets.token_hex(16)
        request_document = {
            "schema_version": 1,
            "rollout_id": rollout_id,
            "batch_index": batch_index,
            "node_ids": list(node_ids),
            "nonce": nonce,
        }
        document = self._request(
            "/internal/v1/updates/grant",
            document=request_document,
        )
        if set(document) != {
            "schema_version",
            "rollout_id",
            "batch_index",
            "node_ids",
            "nonce",
            "grant",
            "signature",
        }:
            raise WorkerAuthorityError("worker authority response is invalid")
        unsigned = dict(document)
        signature = unsigned.pop("signature")
        grant = document.get("grant")
        expected_signature = worker_document_signature(
            self._token,
            unsigned,
            purpose="response",
        )
        if (
            document.get("schema_version") != 1
            or document.get("rollout_id") != rollout_id
            or document.get("batch_index") != batch_index
            or document.get("node_ids") != list(node_ids)
            or document.get("nonce") != nonce
            or not isinstance(grant, Mapping)
            or set(grant) != {"claims", "signature"}
            or not isinstance(signature, str)
            or not secrets.compare_digest(signature, expected_signature)
        ):
            raise WorkerAuthorityError("worker authority response is invalid")
        return dict(grant)

    def current_commit(self) -> str:
        cached = self._require_cached()
        if not cached.current:
            raise WorkerAuthorityError("repository commit is no longer current")
        return cached.commit

    def _evaluate(
        self,
        reconciliation_id: str,
        commit: str,
        plan_digest: str,
        routes: tuple[PublishedRoute, ...],
    ) -> Mapping[str, object]:
        nonce = secrets.token_hex(16)
        request_document = {
            "schema_version": 1,
            "reconciliation_id": reconciliation_id,
            "commit": commit,
            "plan_digest": plan_digest,
            "nonce": nonce,
            "routes": [asdict(route) for route in routes],
        }
        document = self._request(
            "/internal/v1/repository/evaluate",
            document=request_document,
        )
        if set(document) != {
            "schema_version",
            "reconciliation_id",
            "commit",
            "plan_digest",
            "nonce",
            "current",
            "eligible",
            "fleet_evidence_current",
            "routes_sha256",
            "deployments",
            "issued_at",
            "expires_at",
            "signature",
        }:
            raise WorkerAuthorityError("worker authority response is invalid")
        unsigned = dict(document)
        signature = unsigned.pop("signature")
        expected_signature = worker_document_signature(
            self._token,
            unsigned,
            purpose="response",
        )
        expected_routes_digest = published_routes_digest(routes)
        now = self._clock()
        if (
            document["schema_version"] != 1
            or document["reconciliation_id"] != reconciliation_id
            or document["commit"] != commit
            or document["plan_digest"] != plan_digest
            or document["nonce"] != nonce
            or not isinstance(document["current"], bool)
            or not isinstance(document["eligible"], bool)
            or not isinstance(document["fleet_evidence_current"], bool)
            or not isinstance(document["deployments"], list)
            or (document["eligible"] is True and document["current"] is not True)
            or (
                (
                    document["eligible"] is not True
                    or document["current"] is not True
                    or document["fleet_evidence_current"] is not True
                )
                and bool(document["deployments"])
            )
            or document["routes_sha256"] != expected_routes_digest
            or not isinstance(signature, str)
            or not secrets.compare_digest(signature, expected_signature)
            or isinstance(document["issued_at"], bool)
            or not isinstance(document["issued_at"], int)
            or isinstance(document["expires_at"], bool)
            or not isinstance(document["expires_at"], int)
            or not document["issued_at"] <= now < document["expires_at"]
            or document["expires_at"] - document["issued_at"]
            > _MAX_ATTESTATION_SECONDS
        ):
            raise WorkerAuthorityError("worker authority response is invalid")
        return document

    def clear(self) -> None:
        """Discard any prior decision before preparing a new tick."""

        self._cached = None

    @staticmethod
    def _parse_deployments(
        document: Mapping[str, object],
    ) -> tuple[LiteLlmDeployment, ...]:
        parsed: list[LiteLlmDeployment] = []
        try:
            for item in document["deployments"]:
                if not isinstance(item, Mapping) or set(item) != {
                    "model_name",
                    "workload",
                    "api_base",
                    "priority",
                    "requests_per_minute",
                    "tokens_per_minute",
                }:
                    raise TypeError
                deployment = LiteLlmDeployment(**item)
                LiteLlmPublisher._validate_hermes_deployment(deployment)
                parsed.append(deployment)
        except (KeyError, TypeError, ValueError) as error:
            raise WorkerAuthorityError(
                "worker authority deployments are invalid"
            ) from error
        if (
            len({item.priority for item in parsed}) != len(parsed)
            or len({item.workload for item in parsed}) != len(parsed)
        ):
            raise WorkerAuthorityError("worker authority deployments are ambiguous")
        return tuple(parsed)

    def prefetch(
        self,
        reconciliation_id: str,
        commit: str,
        plan_digest: str,
        routes: tuple[PublishedRoute, ...],
    ) -> None:
        """Fetch one bounded decision before reconciliation locks are acquired."""

        self.clear()
        document = self._evaluate(
            reconciliation_id,
            commit,
            plan_digest,
            routes,
        )
        deployments = self._parse_deployments(document)
        self._cached = _CachedAuthority(
            reconciliation_id=reconciliation_id,
            commit=commit,
            plan_digest=plan_digest,
            routes_sha256=str(document["routes_sha256"]),
            current=document["current"] is True,
            eligible=document["eligible"] is True,
            fleet_evidence_current=document["fleet_evidence_current"] is True,
            expires_at=int(document["expires_at"]),
            deployments=deployments,
        )

    def _require_cached(self) -> _CachedAuthority:
        cached = self._cached
        now = self._clock()
        if (
            cached is None
            or isinstance(now, bool)
            or not isinstance(now, int)
            or not now < cached.expires_at
        ):
            self.clear()
            raise WorkerAuthorityError("worker authority decision is unavailable")
        return cached

    def authorized(
        self,
        reconciliation_id: str,
        commit: str,
        plan_digest: str,
        routes: tuple[PublishedRoute, ...],
    ) -> bool:
        cached = self._require_cached()
        if (
            not secrets.compare_digest(cached.reconciliation_id, reconciliation_id)
            or not secrets.compare_digest(cached.commit, commit)
            or not secrets.compare_digest(cached.plan_digest, plan_digest)
            or not secrets.compare_digest(
                cached.routes_sha256,
                published_routes_digest(routes),
            )
        ):
            raise WorkerAuthorityError("worker authority identity changed")
        return cached.current and cached.eligible and cached.fleet_evidence_current

    def authorization_reason(
        self,
        reconciliation_id: str,
        commit: str,
        plan_digest: str,
        routes: tuple[PublishedRoute, ...],
    ) -> bool | str:
        """Return an explicit bounded reason when continuous authority is lost."""

        self.authorized(reconciliation_id, commit, plan_digest, routes)
        cached = self._require_cached()
        if not cached.current:
            return "reconciliation commit is no longer current"
        if not cached.eligible:
            return "reconciliation commit is no longer eligible"
        if not cached.fleet_evidence_current:
            return "fleet acceptance evidence changed since planning"
        return True

    def eligible(self, commit: str) -> bool:
        cached = self._require_cached()
        if not secrets.compare_digest(cached.commit, commit):
            raise WorkerAuthorityError("worker authority commit changed")
        return cached.eligible

    def deployments(
        self,
        commit: str,
        routes: tuple[PublishedRoute, ...],
    ) -> tuple[LiteLlmDeployment, ...]:
        cached = self._require_cached()
        routes_sha256 = published_routes_digest(routes)
        if (
            not secrets.compare_digest(cached.commit, commit)
            or not secrets.compare_digest(cached.routes_sha256, routes_sha256)
        ):
            raise WorkerAuthorityError("worker authority route identity changed")
        if (
            not cached.current
            or not cached.eligible
            or not cached.fleet_evidence_current
        ):
            raise WorkerAuthorityError("repository authority was lost")
        return cached.deployments

@dataclass(frozen=True)
class _CachedAuthority:
    reconciliation_id: str
    commit: str
    plan_digest: str
    routes_sha256: str
    current: bool
    eligible: bool
    fleet_evidence_current: bool
    expires_at: int
    deployments: tuple[LiteLlmDeployment, ...]

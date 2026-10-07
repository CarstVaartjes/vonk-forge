"""Bounded updater compatibility from one complete actual process observation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, Response
from pydantic import Field

from cluster_profiles.runtime_identity import contract_fingerprint

from .operation_api import bounded_error_responses
from .platform_observation import (
    ApiRuntimeObservation,
    CapturedPlatformObservation,
    Digest,
    Source,
)
from .strict_json import StrictModel


class CliUpdateContract(StrictModel):
    observed_at: datetime
    compatibility_schema_sha256: Digest
    api: ApiRuntimeObservation
    expected_worker_contract_sha256: Digest | None
    worker_count: int = Field(ge=0)
    worker_membership_sha256: Digest
    worker_source_sha: Source | None
    worker_contract_sha256: Digest | None
    worker_compatibility: Literal["compatible", "unknown", "incompatible"]
    worker_issue: (
        Literal[
            "worker-observation-unavailable",
            "worker-provenance-unavailable",
            "worker-source-mixed",
            "worker-contract-mixed",
            "api-worker-contract-unavailable",
            "worker-contract-incompatible",
        ]
        | None
    )


def cli_update_contract_schema() -> dict[str, Any]:
    return CliUpdateContract.model_json_schema()


def summarize_cli_update_contract(
    capture: CapturedPlatformObservation,
) -> CliUpdateContract:
    observation = capture.observation
    workers = sorted(
        observation.workers or [], key=lambda worker: worker.process_instance_id
    )
    membership = json.dumps(
        [worker.model_dump(mode="json") for worker in workers],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    sources = {worker.source_sha for worker in workers}
    contracts = {worker.worker_contract_sha256 for worker in workers}
    source = next(iter(sources)) if len(sources) == 1 else None
    contract = next(iter(contracts)) if len(contracts) == 1 else None
    expected = capture.identity.worker_contract_sha256
    issue: (
        Literal[
            "worker-observation-unavailable",
            "worker-provenance-unavailable",
            "worker-source-mixed",
            "worker-contract-mixed",
            "api-worker-contract-unavailable",
            "worker-contract-incompatible",
        ]
        | None
    ) = None
    if not workers or observation.worker_issue == "worker-observation-unavailable":
        issue = "worker-observation-unavailable"
    elif observation.worker_issue is not None or None in sources or None in contracts:
        issue = "worker-provenance-unavailable"
    elif len(sources) != 1:
        issue = "worker-source-mixed"
    elif len(contracts) != 1:
        issue = "worker-contract-mixed"
    elif expected is None:
        issue = "api-worker-contract-unavailable"
    elif contract != expected:
        issue = "worker-contract-incompatible"
    return CliUpdateContract(
        observed_at=observation.observed_at,
        compatibility_schema_sha256=contract_fingerprint(cli_update_contract_schema()),
        api=observation.api,
        expected_worker_contract_sha256=expected,
        worker_count=len(workers),
        worker_membership_sha256=hashlib.sha256(membership).hexdigest(),
        worker_source_sha=source,
        worker_contract_sha256=contract,
        worker_compatibility=(
            "compatible"
            if issue is None
            else "incompatible"
            if issue == "worker-contract-incompatible"
            else "unknown"
        ),
        worker_issue=issue,
    )


def install_cli_update_contract_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    capture: Callable[[], CapturedPlatformObservation],
) -> None:
    @app.get(
        "/api/cli/contract",
        response_model=CliUpdateContract,
        operation_id="getCliUpdateContract",
        responses=bounded_error_responses(401, 503),
    )
    def cli_update_contract(
        response: Response, _actor: Any = actor_dependency
    ) -> CliUpdateContract:
        response.headers["Cache-Control"] = "no-store"
        return summarize_cli_update_contract(capture())

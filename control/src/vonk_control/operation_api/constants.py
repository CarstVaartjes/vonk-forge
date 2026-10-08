"""Operation Api: constants."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

COMMIT_PATTERN = r"^[0-9a-f]{40}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,62}$"
NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_ACTIVE_PUBLICATION_STATES = frozenset({"completed"})
_ADMIN_OPERATION_IDS = {
    ("get", "/api/cli/contract"): "getCliUpdateContract",
    ("get", "/api/platform"): "getPlatformObservation",
    ("get", "/api/fleet"): "getFleetStatus",
    ("get", "/api/operations/{operation_id}/evidence"): "getOperationEvidence",
    ("get", "/api/fleet/stream"): "streamFleetEvents",
    (
        "get",
        "/api/recipe/runs/{run_id}/artifact-jobs",
    ): "listArtifactJobsForRun",
    (
        "post",
        "/api/recipe/runs/{run_id}/artifact-jobs",
    ): "createArtifactJob",
    ("get", "/api/artifact-jobs/capabilities"): "getArtifactJobCapabilities",
    ("get", "/api/artifact-jobs/requests/{request_id}"): "getArtifactJobByRequestId",
    ("get", "/api/artifact-jobs/{job_id}"): "getArtifactJobStatus",
    ("put", "/api/artifact-jobs/{job_id}/inputs/{name}"): "uploadArtifactJobInput",
    ("post", "/api/artifact-jobs/{job_id}/finalize"): "finalizeArtifactJob",
    ("post", "/api/artifact-jobs/{job_id}/submit"): "submitArtifactJob",
    ("post", "/api/artifact-jobs/{job_id}/cancel"): "cancelArtifactJob",
    (
        "get",
        "/api/artifact-jobs/{job_id}/results/{name}/{sha256}",
    ): "downloadArtifactJobResult",
    ("get", "/api/operations"): "listOperations",
    ("get", "/api/jobs/{job_id}"): "getJob",
    ("get", "/api/operations/{operation_id}"): "getOperation",
    ("post", "/api/jobs/{job_id}/resume"): "resumeJob",
}
_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=128)]
NodeIdentifier = Annotated[str, Field(pattern=NODE_PATTERN)]
DigestIdentifier = Annotated[str, Field(pattern=DIGEST_PATTERN)]

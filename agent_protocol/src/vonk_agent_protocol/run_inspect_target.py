"""What one read-only run inspection asks the root helper about.

Each variant is one question, with every identity typed. The agent writes one
of these into a ``run-inspect`` runtime request and the helper reads it back;
neither side parses positional words.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .contracts import CanonicalUUID
from .wire_model import MAX_RUN_GENERATION, Digest, WireEnum, WireModel, typed_tag

OciDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
NoNulText = Annotated[str, Field(pattern=r"^[^\x00]*$")]


class RunInspectKind(WireEnum):
    INSTALLATION_IN_USE = "installation-in-use"
    RUN_NAME = "run-name"
    JOB_IDENTITY = "job-identity"
    RUNTIME_IDENTITY = "runtime-identity"
    STOP_IDENTITY = "stop-identity"
    CONTAINER_RUN = "container-run"


class InstallationInUseTarget(WireModel):
    """Does any runtime container still use this installation?"""

    kind: Literal[RunInspectKind.INSTALLATION_IN_USE] = typed_tag()
    installation_id: CanonicalUUID


class RunNameTarget(WireModel):
    """The container named for this run id, by name alone.

    Without the whole run identity the container cannot be proven to be the
    run's own, so its output is never read.
    """

    kind: Literal[RunInspectKind.RUN_NAME] = typed_tag()
    run_id: CanonicalUUID


class _RunIdentityTarget(WireModel):
    runtime_id: CanonicalUUID
    run_id: CanonicalUUID
    installation_id: CanonicalUUID
    run_generation: Annotated[int, Field(ge=1, le=MAX_RUN_GENERATION, strict=True)]
    plan_digest: Digest


class JobIdentityTarget(_RunIdentityTarget):
    """One job container, by its whole identity."""

    kind: Literal[RunInspectKind.JOB_IDENTITY] = typed_tag()


class RuntimeIdentityTarget(_RunIdentityTarget):
    """One Start container, by its whole identity."""

    kind: Literal[RunInspectKind.RUNTIME_IDENTITY] = typed_tag()


class StopIdentityTarget(_RunIdentityTarget):
    """One Stop's effect: the container is gone and its generation fenced.

    With ``cancel_pending_start`` the cancelled generation fence must also be
    durable and no Start still in flight.
    """

    kind: Literal[RunInspectKind.STOP_IDENTITY] = typed_tag()
    cancel_pending_start: bool


class ContainerRunTarget(WireModel):
    """The exact container one Start's request describes."""

    kind: Literal[RunInspectKind.CONTAINER_RUN] = typed_tag()
    archive_sha256: Digest
    registry_index_digest: OciDigest
    platform_manifest_digest: OciDigest
    image_reference: NoNulText
    # The `docker run` command line, validated by the helper before use.
    command: list[NoNulText]


type RunInspectTarget = Annotated[
    InstallationInUseTarget
    | RunNameTarget
    | JobIdentityTarget
    | RuntimeIdentityTarget
    | StopIdentityTarget
    | ContainerRunTarget,
    Field(discriminator="kind"),
]

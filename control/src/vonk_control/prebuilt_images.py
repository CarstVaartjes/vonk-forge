"""Prebuilt recipe runtime images.

The recipe library builds every distinct recipe image on a GitHub-hosted
ARM64 runner when a recipe is published or updated, pushes it to GHCR, and
records ``{reference, build_key}`` beside the recipe in the signed catalog
index.  ``reference`` pins the image by manifest digest; ``build_key`` names
the executable build inputs the image was produced from.

The Controller prefers that image.  When the signed catalog pins an image for
the revision's content digest, build planning skips Spark build admission (no
Spark memory or disk is used) and the Controller pulls the pinned digest into
its image cache instead of dispatching a Spark build.  The Controller never
reads ``build_key``: the manifest digest is the image's identity, and the key
is only the recipes CI's handle for finding or skipping a build.  The digest
is verified once, where the bytes enter: skopeo pulls by digest and checks the
manifest and every blob against it.  The resulting archive then takes exactly
the path a Spark-built archive takes (receipt, authorization, distribution), so
a prebuilt image and a Spark-built image for the same inputs are
interchangeable.  A missing or failed prebuilt image falls back to the Spark
build, and the fallback is never silent: every plan carries a
:class:`PrebuiltDecision` that names why the image was or was not used.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, true, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import read_recipe_display
from vonk_forge_contracts.catalog_index import PREBUILT_REFERENCE_PATTERN

from vonk_agent_protocol.agent_operations import AgentFailureKind, AgentFailureResult
from vonk_agent_protocol.build_import import RecipeBuildEvidence
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.reason_codes.images import PrebuiltImageCode
from vonk_control.image_store.collection import GRACE
from vonk_control.image_store.oci import (
    IMAGE_CACHE_DIRECTORY,
    STORE_BUSY,
    OciImageStore,
    OciImageStoreError,
    StoreUnknown,
)
from vonk_control.lifecycle.image_availability import PrebuiltImageAdapter
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.jobs import Job
from vonk_control.recipe_builds.cancellation import build_cancellation
from vonk_control.recipe_builds.common import (
    inspect_package_source_policy,
)
from vonk_control.recipe_operations.execution_contract import (
    RecipeExecutionContractError,
    StoredBuildPolicyReport,
    parse_stored_build_policy,
)
from vonk_control.recipe_operations.observation_helpers import record_build_evidence
from vonk_control.recipe_packages.client import load_recipe_package
from vonk_control.recipe_packages.library_types import RecipeLibraryItem
from vonk_control.source_bundles import GeneratedSourceBundle, parse_source_bundle
from vonk_control.source_policy import SourcePolicyError
from vonk_control.stored.states import RecipeBuildState

_LOGGER = logging.getLogger(__name__)

PREBUILT_BUILD_KIND = "recipe.build.v1"
# A Controller pull of one image; renewed while the pull runs.
_CLAIM_LEASE = timedelta(minutes=5)
# One uploaded archive that fails to convert is retried with a growing backoff
# so it never holds back the archives behind it. The bound is the image-store
# collection, which drops the upload 24 h after its build succeeded; a healed
# fault converts on the next due attempt.
_CONVERSION_BACKOFF = timedelta(minutes=10)
_CONVERSION_BACKOFF_CAP = timedelta(hours=2)
_REFERENCE = re.compile(PREBUILT_REFERENCE_PATTERN)

PREBUILT_NOT_PINNED = PrebuiltImageCode.NOT_PINNED


def _library_recipe(
    library_root: Path, index: Mapping[str, object], entry: Mapping[str, object]
) -> tuple[RecipeLibraryItem, GeneratedSourceBundle]:
    """Load one indexed recipe package with its verified build-context bundle."""

    document = entry["document"]
    assert isinstance(document, Mapping)
    identity = document["identity"]
    package = entry["package"]
    display = entry["display"]
    assert isinstance(identity, Mapping)
    assert isinstance(package, Mapping)
    assert isinstance(display, Mapping)
    item = load_recipe_package(
        library_root / str(package["path"]),
        package_sha256=str(package["sha256"]),
        publisher=str(identity["publisher"]),
        slug=str(identity["slug"]),
        recipe_content_sha256=str(entry["content_sha256"]),
        library_commit=str(index["source_commit"]),
        source_path=str(entry["source_path"]),
        display=read_recipe_display(display),
    )
    if item.source_bundle is None or item.source_bundle_sha256 is None:
        raise ValueError("package has no build context")
    return item, parse_source_bundle(item.source_bundle)


def _require_source_policy(
    document: Mapping[str, object],
    bundle: GeneratedSourceBundle,
    source_sha256: str | None,
) -> None:

    report = inspect_package_source_policy(
        document, bundle, source_sha256=source_sha256
    )
    if not report.passed:
        raise SourcePolicyError(report)


def _reason(error: Exception) -> str:
    report = getattr(error, "report", None)
    text = report.describe() if report is not None else str(error)
    return text[:600]


def library_source_policy_failures(library_root: Path) -> dict[str, str]:
    """Recipes of a built library that cannot be loaded or that the policy refuses.

    Maps each refused recipe slug to the findings.  Recipe library validation
    fails on a non-empty result: a recipe the Controller cannot build or pull
    must not pass validation, whether or not it has a prebuilt image.
    """

    index = json.loads((library_root / "catalog-index.json").read_text("utf-8"))
    failures: dict[str, str] = {}
    for entry in index.get("recipes", []):
        slug = str(entry.get("document", {}).get("identity", {}).get("slug", ""))
        try:
            item, bundle = _library_recipe(library_root, index, entry)
            _require_source_policy(item.document, bundle, item.source_bundle_sha256)
        except UnknownOutcomeError as error:
            # No build was accepted and no resource is retained by this report.
            failures[slug] = _reason(error)
        except Exception as error:  # noqa: BLE001 - report every recipe, not the first
            failures[slug] = _reason(error)
    return failures


def prebuilt_reference(job: Job) -> str | None:
    """The pinned image a Controller-executed build job pulls, if any."""

    payload = job.payload if isinstance(job.payload, Mapping) else {}  # pyright: ignore[reportUnnecessaryIsInstance]  # stored JSON may not have the shape its column type names
    reference = payload.get("prebuilt_image")
    if isinstance(reference, str) and _REFERENCE.fullmatch(reference):
        return reference
    return None


def policy_prebuilt_reference(policy: object) -> str | None:
    """The pinned image a stored build policy says the Controller pulls."""

    try:
        document = (
            policy
            if isinstance(policy, StoredBuildPolicyReport)
            else parse_stored_build_policy(policy)
        )
    except RecipeExecutionContractError:
        return None
    reference = document.prebuilt_image
    if isinstance(reference, str) and _REFERENCE.fullmatch(reference):
        return reference
    return None


class PrebuiltImageImporter:
    """Execute Controller-side prebuilt image builds.

    A build planned from a prebuilt image is queued as an ordinary
    ``recipe.build.v1`` job with no Spark operation.  This importer claims
    such jobs, pulls the image off the worker loop, and records the same
    evidence a Spark upload records.  A claim is a renewable lease in the job
    payload, so a restarted Controller resumes an interrupted pull.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        artifact_root: Path,
        *,
        clock: Callable[[], datetime],
        store: OciImageStore | None = None,
        owner: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._image_cache = artifact_root / IMAGE_CACHE_DIRECTORY
        self._clock = clock
        self._lifecycle = PrebuiltImageAdapter(clock=clock)
        self._store = store or OciImageStore(artifact_root)
        self._owner = owner or f"prebuilt-{uuid.uuid4()}"
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="prebuilt-image"
        )
        self._active: Future[None] | None = None
        # archive sha256 -> (failed attempts, earliest next attempt); in memory, so a
        # restart simply starts the backoff again.
        self._conversion_failures: dict[str, tuple[int, datetime]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def tick(self, job_id: str | None = None) -> int:
        """Start at most one import; never block the worker loop."""

        with self._lock:
            if self._active is not None and not self._active.done():
                return 0
            requested_id = job_id
            job_id = self._claim(job_id)
            if job_id is not None:
                self._active = self._executor.submit(self.run, job_id)
                return 1
            if requested_id is not None:
                return 0
            build_id = self._uploaded_build()
            if build_id is None:
                return 0
            self._active = self._executor.submit(self.convert_upload, build_id)
            return 1

    def run_pending(self) -> int:
        """Run one import in the calling thread (tests, tools)."""

        job_id = self._claim()
        if job_id is not None:
            self.run(job_id)
            return 1
        build_id = self._uploaded_build()
        if build_id is None:
            return 0
        self.convert_upload(build_id)
        return 1

    def _uploaded_build(self) -> str | None:
        """A succeeded Spark build whose upload is not in the layout yet.

        A Spark build uploads one Docker archive, named by its sha256, beside
        the layout. Conversion runs only after the build succeeded: the
        agent's terminal evidence is compared against the uploaded identity
        first, and only then is the row rewritten to the stored image.
        """
        now = self._clock()
        for archive_sha256 in list(self._conversion_failures):
            if not (self._image_cache / archive_sha256).is_file():
                del self._conversion_failures[archive_sha256]
        with self._sessions() as session:
            for build_id, layout in session.execute(
                select(RecipeBuild.id, RecipeBuild.oci_layout_sha256)
                .where(
                    RecipeBuild.state == RecipeBuildState.SUCCEEDED,
                    RecipeBuild.oci_layout_sha256.is_not(None),
                )
                .order_by(RecipeBuild.updated_at, RecipeBuild.id)
            ):
                if (
                    isinstance(layout, str)
                    and (self._image_cache / layout).is_file()
                    and self._conversion_due(layout, now)
                ):
                    return str(build_id)
        return None

    def _conversion_due(self, archive_sha256: str, now: datetime) -> bool:
        _attempts, retry_at = self._conversion_failures.get(archive_sha256, (0, now))
        return retry_at <= now

    def _record_conversion_failure(
        self,
        build_id: str,
        archive_sha256: str,
        failure: StoreUnknown,
        expires_at: datetime,
    ) -> None:
        now = self._clock()
        attempts = self._conversion_failures.get(archive_sha256, (0, now))[0] + 1
        retry_at = now + min(_CONVERSION_BACKOFF * attempts, _CONVERSION_BACKOFF_CAP)
        self._conversion_failures[archive_sha256] = (attempts, retry_at)
        _LOGGER.warning(
            "uploaded build %s: %s: %s (attempt %d, next attempt %s)",
            build_id,
            failure.code,
            failure.detail,
            attempts,
            retry_at.isoformat(),
        )
        # Stays true across a restart and until the collection drops the upload.
        detail = (
            f"conversion failed: {failure.code}; retried until the upload "
            f"expires at {(expires_at + GRACE).isoformat()}"
        )
        with self._sessions.begin() as session:
            session.execute(
                update(RecipeBuild)
                .where(
                    RecipeBuild.id == build_id,
                    RecipeBuild.oci_layout_sha256 == archive_sha256,
                )
                .values(error=detail[:512])
            )

    def convert_upload(self, build_id: str) -> None:
        """Move a Spark build's uploaded archive into the layered store."""

        with self._sessions() as session:
            build = session.get(RecipeBuild, build_id)
            archive_sha256 = None if build is None else build.oci_layout_sha256
            succeeded_at = None if build is None else build.updated_at
        if archive_sha256 is None or succeeded_at is None:
            return
        archive = self._image_cache / archive_sha256
        image = self._store.import_archive(archive)
        if isinstance(image, StoreUnknown):
            if image.code != STORE_BUSY:
                # A busy store is not this archive's fault: it simply goes again.
                self._record_conversion_failure(
                    build_id, archive_sha256, image, succeeded_at
                )
            return
        self._conversion_failures.pop(archive_sha256, None)
        with self._sessions.begin() as session:
            converted = session.execute(
                update(RecipeBuild)
                .where(
                    RecipeBuild.id == build_id,
                    RecipeBuild.oci_layout_sha256 == archive_sha256,
                )
                .values(
                    image_digest=image.manifest_digest,
                    oci_layout_sha256=image.manifest_digest.removeprefix("sha256:"),
                    image_bytes=image.stored_bytes,
                    error=None,
                    updated_at=self._clock(),
                )
                .returning(RecipeBuild.id)
            )
            if converted.first() is None:
                return
        archive.unlink(missing_ok=True)
        _LOGGER.info(
            "stored Spark build %s as runtime image %s",
            build_id,
            image.manifest_digest,
        )

    def _claim(self, job_id: str | None = None) -> str | None:

        now = self._clock()
        with self._sessions.begin() as session:
            candidates = session.scalars(
                select(Job)
                .where(
                    Job.kind == PREBUILT_BUILD_KIND,
                    Job.id == job_id if job_id is not None else true(),
                    Job.state == LifecycleState.RUNNING,
                    # Only Controller pulls; Spark builds are never touched.
                    Job.payload["prebuilt_image"].as_string().is_not(None),
                )
                .order_by(Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
            )
            for job in candidates:
                if (
                    prebuilt_reference(job) is None
                    or build_cancellation(job) is not None
                ):
                    continue
                payload = dict(job.payload)
                claim_until = payload.get("prebuilt_claim_until")
                if isinstance(claim_until, str) and (
                    datetime.fromisoformat(claim_until) > now
                ):
                    continue
                job.payload = payload | {
                    "prebuilt_claim_owner": self._owner,
                    "prebuilt_claim_until": (now + _CLAIM_LEASE).isoformat(),
                }
                job.updated_at = now
                return job.id
        return None

    def _renew(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(_CLAIM_LEASE.total_seconds() / 3):
            with self._sessions.begin() as session:
                job = session.get(Job, job_id, with_for_update=True)
                if (
                    job is None
                    or job.state != LifecycleState.RUNNING
                    or job.payload.get("prebuilt_claim_owner") != self._owner
                ):
                    return
                job.payload = dict(job.payload) | {
                    "prebuilt_claim_until": (self._clock() + _CLAIM_LEASE).isoformat()
                }

    def run(self, job_id: str) -> None:

        with self._sessions() as session:
            job = session.get(Job, job_id)
            if (
                job is None
                or job.state != LifecycleState.RUNNING
                or job.payload.get("prebuilt_claim_owner") != self._owner
                or build_cancellation(job) is not None
            ):
                return
            reference = prebuilt_reference(job)
            build_id = job.payload.get("owner_id")
            node_id = job.payload.get("prebuilt_node_id")
        if (
            reference is None
            or not isinstance(build_id, str)
            or not isinstance(node_id, str)
        ):
            return
        stop = threading.Event()
        renewer = threading.Thread(target=self._renew, args=(job_id, stop), daemon=True)
        renewer.start()
        try:
            _LOGGER.info("pulling prebuilt runtime image %s", reference)
            image = self._store.import_reference(reference)
        except (OciImageStoreError, OSError) as error:
            # A malformed reference or a local fault: this pull is over.
            self._fail(job_id, build_id, node_id, reference, error)
        else:
            if isinstance(image, StoreUnknown):
                if image.code == STORE_BUSY:
                    self._release(job_id)
                else:
                    self._fail(job_id, build_id, node_id, reference, image)
            else:
                self._finish(
                    job_id,
                    build_id,
                    node_id,
                    evidence=RecipeBuildEvidence(
                        image_bytes=image.stored_bytes,
                        image_digest=image.manifest_digest,
                        oci_layout_sha256=image.manifest_digest.removeprefix("sha256:"),
                    ),
                )
        finally:
            stop.set()

    def _fail(
        self,
        job_id: str,
        build_id: str,
        node_id: str,
        reference: str,
        error: OciImageStoreError | StoreUnknown | OSError,
    ) -> None:
        code = PrebuiltImageCode.PULL_FAILED
        detail = str(getattr(error, "detail", error)) or type(error).__name__
        _LOGGER.warning(
            "prebuilt runtime image %s is unavailable (%s); the next plan "
            "builds on a Spark instead",
            reference,
            detail,
        )
        self._finish(job_id, build_id, node_id, failure=(code, detail))

    def _release(self, job_id: str) -> None:
        """Give a claimed import back so the next tick retries it."""

        with self._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None or job.payload.get("prebuilt_claim_owner") != self._owner:
                return
            job.payload = {
                key: value
                for key, value in job.payload.items()
                if key not in {"prebuilt_claim_owner", "prebuilt_claim_until"}
            }
            job.updated_at = self._clock()

    def _finish(
        self,
        job_id: str,
        build_id: str,
        node_id: str,
        *,
        evidence: RecipeBuildEvidence | None = None,
        failure: tuple[str, str] | None = None,
    ) -> None:

        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update=True)
            build = session.get(RecipeBuild, build_id, with_for_update=True)
            if (
                job is None
                or build is None
                or job.state != LifecycleState.RUNNING
                or job.payload.get("prebuilt_claim_owner") != self._owner
            ):
                return
            payload = {
                key: value
                for key, value in job.payload.items()
                if key not in {"prebuilt_claim_owner", "prebuilt_claim_until"}
            }
            if evidence is not None and not record_build_evidence(
                session, build, evidence.model_dump(mode="json"), now=now
            ):
                # The pulled image's evidence does not hold: the import fails
                # like a failed pull (it is retried), it is never raised.
                evidence = None
                failure = (
                    PrebuiltImageCode.EVIDENCE_INVALID,
                    "the pulled image evidence does not match the build",
                )
            if evidence is not None:
                document = evidence.model_dump(mode="json")
                self._lifecycle.finish(job, ok=True, now=now)
                job.result = {
                    "successful_nodes": [node_id],
                    "failed_nodes": [],
                    "node_evidence": {node_id: document},
                }
            else:
                assert failure is not None
                code, detail = failure
                build.state = RecipeBuildState.FAILED.value
                build.error = f"{code}: {detail}"[:512]
                build.updated_at = now
                self._lifecycle.finish(job, ok=False, now=now)
                job.result = {
                    "successful_nodes": [],
                    "failed_nodes": [node_id],
                    "node_evidence": {
                        node_id: AgentFailureResult(
                            error_code=code,
                            summary=f"prebuilt image pull failed: {detail}"[:1024],
                            failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                            retry_after_seconds=5,
                            stage="prebuilt-pull",
                        ).model_dump(mode="json", exclude_none=True)
                    },
                }
            job.payload = payload
            job.status_reason = None if evidence is not None else build.error
            job.updated_at = now


__all__ = [
    "PREBUILT_NOT_PINNED",
    "PrebuiltImageImporter",
    "library_source_policy_failures",
    "policy_prebuilt_reference",
    "prebuilt_reference",
]

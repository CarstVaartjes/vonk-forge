"""Prebuilt recipe runtime images.

The recipe library builds every distinct recipe image on a GitHub-hosted
ARM64 runner when a recipe is published or updated, pushes it to GHCR, and
records ``{reference, build_key}`` beside the recipe in the signed catalog
index.  ``reference`` pins the image by manifest digest; ``build_key`` names
the executable build inputs the image was produced from.

The Controller prefers that image.  When a revision's own build key matches
the catalog's, build planning skips Spark build admission (no Spark memory or
disk is used) and the Controller pulls the pinned digest into its image cache
instead of dispatching a Spark build.  The digest is verified once, where the
bytes enter: skopeo pulls by digest and checks the manifest and every blob
against it.  The resulting archive then takes exactly the path a Spark-built
archive takes (receipt, authorization, distribution), so a prebuilt image and
a Spark-built image for the same inputs are interchangeable.  A missing,
mismatched or failed prebuilt image falls back to the Spark build, and the
fallback is never silent: every plan carries a :class:`PrebuiltDecision` that
names why the image was or was not used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    canonical_message,
)
from vonk_agent_protocol.build_import import RecipeBuildEvidence, RecipeBuildOptions
from vonk_forge_contracts import RecipeDefinition, read_recipe

from .catalog_revision_contract import (
    PREBUILT_REFERENCE_PATTERN,
    BuildSecurityProjection,
    PrebuiltImage,
)
from .models import Job, RecipeBuild
from .oci_image_store import (
    IMAGE_CACHE_DIRECTORY,
    STORE_BUSY,
    OciImageStore,
    OciImageStoreError,
)

_LOGGER = logging.getLogger(__name__)

PREBUILT_KEY_SCHEMA_VERSION = 1
PREBUILT_BUILD_KIND = "recipe.build.v1"
# A Controller pull of one image; renewed while the pull runs.
_CLAIM_LEASE = timedelta(minutes=5)
_REFERENCE = re.compile(PREBUILT_REFERENCE_PATTERN)

# Why a plan did or did not use the catalog's prebuilt image.  The codes are
# stable: they are stored with the build, logged, and shown as operation
# blockers, so an operator can tell a Spark build from an image pull.
PREBUILT_USED = "prebuilt.used"
PREBUILT_NOT_PINNED = "prebuilt.not_pinned"
PREBUILT_KEY_MISMATCH = "prebuilt.build_key_mismatch"
PREBUILT_RECENT_PULL_FAILURE = "prebuilt.pull_failed_recently"


@dataclass(frozen=True, slots=True)
class PrebuiltDecision:
    """The named outcome of choosing between a prebuilt image and a Spark build."""

    code: str
    detail: str

    @property
    def used(self) -> bool:
        return self.code == PREBUILT_USED

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def executable_build_key(identity: Mapping[str, object]) -> str:
    """Name the build inputs that produce the image bytes.

    ``identity`` is a build input identity from
    :func:`vonk_control.recipe_builds.derive_build_input_identity`.  The key
    keeps the inputs a build consumes (source bundle, Dockerfile, pinned base
    images, build options and capabilities, runtime adapter) and drops the
    ones that only scope reuse (builder binary, egress hosts, rebuild
    settings, topology, model artifacts).  Two recipes that build the same
    adapter directory the same way therefore share one prebuilt image.
    """

    execution = identity.get("execution_build")
    if not isinstance(execution, Mapping):
        raise TypeError("build identity has no executable build")
    document = {
        "schema_version": PREBUILT_KEY_SCHEMA_VERSION,
        "artifact_format": identity.get("artifact_format"),
        "source_bundle_sha256": identity.get("source_bundle_sha256"),
        "base_images": identity.get("base_images"),
        "base_image": execution.get("base_image"),
        "dockerfile": execution.get("dockerfile"),
        "options": execution.get("options"),
        "security": execution.get("security"),
        "runtime_adapter": identity.get("runtime_adapter"),
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def package_build_intent(
    document: Mapping[str, object] | RecipeDefinition,
    *,
    source_bundle_sha256: str,
    dockerfile_payload: bytes,
) -> dict[str, object]:
    """Derive the build input identity CI builds from, without a database.

    This is the same derivation :meth:`RecipeBuildService.resolve` applies to
    a stored revision, fed from a recipe package instead.  Only the fields
    :func:`executable_build_key` reads matter for a prebuilt image.
    """

    from .catalog_entities import build_policy_projection
    from .recipe_builds import (
        canonical_build,
        derive_build_input_identity,
    )
    from .runtime_adapters import resolve_runtime_adapter
    from .source_policy import dockerfile_base_images

    recipe = (
        document if isinstance(document, RecipeDefinition) else read_recipe(document)
    )
    raw = document if isinstance(document, Mapping) else recipe.model_dump(mode="json")
    policy = build_policy_projection(recipe)
    build = canonical_build(
        raw,
        options=RecipeBuildOptions.model_validate_json(
            canonical_message(policy["build_options"])
        ),
        security=BuildSecurityProjection.model_validate_json(
            canonical_message(policy["build_security"])
        ),
    )
    adapter = resolve_runtime_adapter(recipe.runtime.engine, recipe.topology)
    return derive_build_input_identity(
        build,
        source_bundle_sha256=source_bundle_sha256,
        builder_binary_digest=None,
        base_images=list(dockerfile_base_images(dockerfile_payload)),
        runtime_adapter=adapter.document(),
    )


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and "\0" not in value:
        return value
    raise ValueError("build environment value is not a scalar")


def podman_build_arguments(
    options: RecipeBuildOptions,
    *,
    capabilities: Sequence[str],
    processes: int,
    dockerfile: str,
    context: str,
) -> list[str]:
    """The ``podman build`` arguments a Spark builder uses for these options.

    Mirrors ``rust/crates/vonk-agent/src/recipe_builder.rs`` (the build
    stage), so an image built in CI is produced under the same flags as a
    Spark build.  Paths are relative to the caller's working directory.
    """

    arguments = [
        "--no-cache",
        "--pull=never",
        "--platform",
        "linux/arm64",
        "--file",
        f"{context}/{dockerfile}",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        f"--ulimit=nproc={processes}:{processes}",
        f"--format={options.format}",
        f"--identity-label={_scalar(options.identity_label)}",
        f"--jobs={options.jobs}",
        f"--disable-compression={_scalar(options.layer_compression == 'disabled')}",
        f"--layers={_scalar(options.layers)}",
        f"--no-hostname={_scalar(options.no_hostname)}",
        f"--no-hosts={_scalar(options.no_hosts)}",
        f"--omit-history={_scalar(options.omit_history)}",
        f"--shm-size={options.shm_bytes}",
        f"--skip-unused-stages={_scalar(options.skip_unused_stages)}",
    ]
    for item in options.additional_contexts:
        arguments += ["--build-context", f"{item.name}={context}/{item.path}"]
    for flag, entries in (
        ("--annotation", options.annotations),
        ("--label", options.labels),
        ("--layer-label", options.layer_labels),
    ):
        for entry in entries:
            arguments += [flag, f"{entry.name}={entry.value}"]
    for variable in options.environment:
        arguments += ["--env", f"{variable.name}={_scalar(variable.value)}"]
    if options.ignorefile is not None:
        arguments += ["--ignorefile", f"{context}/{options.ignorefile}"]
    for feature in options.os_features:
        arguments += ["--os-feature", str(feature)]
    if options.os_version is not None:
        arguments += ["--os-version", str(options.os_version)]
    if options.squash == "new":
        arguments.append("--squash")
    elif options.squash == "all":
        arguments.append("--squash-all")
    if options.timestamp is not None:
        arguments.append(f"--timestamp={options.timestamp}")
    for name in options.unset_environment:
        arguments += ["--unsetenv", str(name)]
    for name in options.unset_labels:
        arguments += ["--unsetlabel", str(name)]
    arguments += [f"--cap-add={capability}" for capability in capabilities]
    return arguments


def write_library_image_plan(library_root: Path, output: Path) -> dict[str, object]:
    """Plan one image per distinct build key for a built recipe library.

    ``library_root`` is a recipe checkout after ``tools/build-catalog-index``
    wrote ``catalog-index.json`` and ``packages/``.  For every distinct key
    this writes ``<output>/<key>/context/`` (the exact build context a Spark
    receives) and returns the build arguments, the adapter stage and the
    recipes that share the image.  Recipes whose package cannot be planned
    are listed under ``skipped`` with the reason; they simply get no
    prebuilt image.
    """

    import io
    import tarfile

    from .catalog_entities import build_policy_projection
    from .recipe_packages import load_recipe_package
    from .runtime_adapters import (
        RUNTIME_ADAPTER_DIGEST_LABEL,
        RUNTIME_ADAPTER_LABEL,
        RUNTIME_INTERFACE_LABEL,
        RUNTIME_INTERFACE_LABEL_VALUE,
        resolve_runtime_adapter,
    )

    index = json.loads((library_root / "catalog-index.json").read_text("utf-8"))
    images: dict[str, dict[str, object]] = {}
    skipped: list[dict[str, str]] = []
    for entry in index.get("recipes", []):
        document = entry.get("document", {})
        identity = document.get("identity", {}) if isinstance(document, Mapping) else {}
        slug = str(identity.get("slug", ""))
        try:
            package = entry["package"]
            item = load_recipe_package(
                library_root / str(package["path"]),
                package_sha256=str(package["sha256"]),
                publisher=str(identity["publisher"]),
                slug=slug,
                recipe_content_sha256=str(entry["content_sha256"]),
                library_commit=str(index["source_commit"]),
                source_path=str(entry["source_path"]),
            )
            if item.source_bundle is None or item.source_bundle_sha256 is None:
                raise ValueError("package has no build context")
            recipe = read_recipe(item.document)
            with tarfile.open(fileobj=io.BytesIO(item.source_bundle)) as bundle:
                files = {
                    member.name: bundle.extractfile(member).read()  # type: ignore[union-attr]
                    for member in bundle.getmembers()
                    if member.isfile()
                }
            intent = package_build_intent(
                item.document,
                source_bundle_sha256=item.source_bundle_sha256,
                dockerfile_payload=files[
                    _bundle_relative(
                        recipe.execution.build.dockerfile,
                        recipe.execution.build.context.path,
                    )
                ],
            )
            key = executable_build_key(intent)
        except Exception as error:  # noqa: BLE001 - one recipe never blocks the others
            skipped.append({"slug": slug, "reason": str(error)[:300]})
            continue
        recipe_entry = {
            "publisher": recipe.identity.publisher,
            "slug": recipe.identity.slug,
            "version": recipe.release.version if recipe.release else None,
            "content_sha256": item.content_sha256,
            "source_path": item.source_path,
            "context": recipe.execution.build.context.path,
        }
        if key in images:
            images[key]["recipes"].append(recipe_entry)  # type: ignore[union-attr]
            continue
        policy = build_policy_projection(recipe)
        options = RecipeBuildOptions.model_validate_json(
            canonical_message(policy["build_options"])
        )
        resources = policy["build_resources"]
        security = policy["build_security"]
        assert isinstance(resources, Mapping) and isinstance(security, Mapping)
        adapter = resolve_runtime_adapter(recipe.runtime.engine, recipe.topology)
        directory = output / key / "context"
        directory.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(item.source_bundle)) as bundle:
            bundle.extractall(directory, filter="data")
        execution = intent["execution_build"]
        assert isinstance(execution, Mapping)
        images[key] = {
            "build_key": key,
            "recipes": [recipe_entry],
            "base_images": [
                str(image["reference"])
                for image in intent["base_images"]  # type: ignore[union-attr]
            ],
            "recipe_build_arguments": podman_build_arguments(
                options,
                capabilities=list(security["capabilities"]),
                processes=int(resources["processes"]),
                dockerfile=str(execution["dockerfile"]),
                context="context",
            ),
            "adapter_containerfile": adapter.containerfile,
            "adapter_build_argument": "VONK_RECIPE_IMAGE",
            "adapter_labels": dict(adapter.labels()),
            "adapter_build_arguments": [
                "--network=none",
                "--no-cache",
                "--pull=never",
                "--platform",
                "linux/arm64",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                f"--format={options.format}",
                *(
                    f"--label={name}={value}"
                    for name, value in (
                        (RUNTIME_INTERFACE_LABEL, RUNTIME_INTERFACE_LABEL_VALUE),
                        (RUNTIME_ADAPTER_LABEL, adapter.adapter_id),
                        (RUNTIME_ADAPTER_DIGEST_LABEL, adapter.digest),
                    )
                ),
                *(f"--cap-add={item}" for item in security["capabilities"]),
            ],
        }
    return {
        "schema_version": 1,
        "source_commit": index.get("source_commit"),
        "images": sorted(images.values(), key=lambda value: str(value["build_key"])),
        "skipped": skipped,
    }


def _bundle_relative(dockerfile: str, context: str) -> str:
    return dockerfile.removeprefix(context.rstrip("/") + "/")


def prebuilt_reference(job: Job) -> str | None:
    """The pinned image a Controller-executed build job pulls, if any."""

    payload = job.payload if isinstance(job.payload, Mapping) else {}
    reference = payload.get("prebuilt_image")
    if isinstance(reference, str) and _REFERENCE.fullmatch(reference):
        return reference
    return None


def policy_prebuilt_reference(policy: object) -> str | None:
    """The pinned image a stored build policy says the Controller pulls."""

    reference = policy.get("prebuilt_image") if isinstance(policy, Mapping) else None
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
        self._store = store or OciImageStore(artifact_root)
        self._owner = owner or f"prebuilt-{uuid.uuid4()}"
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="prebuilt-image"
        )
        self._active: Future[None] | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def tick(self) -> int:
        """Start at most one import; never block the worker loop."""

        with self._lock:
            if self._active is not None and not self._active.done():
                return 0
            job_id = self._claim()
            if job_id is not None:
                self._active = self._executor.submit(self.run, job_id)
                return 1
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
        with self._sessions() as session:
            for build_id, layout in session.execute(
                select(RecipeBuild.id, RecipeBuild.oci_layout_sha256)
                .where(
                    RecipeBuild.state == "succeeded",
                    RecipeBuild.oci_layout_sha256.is_not(None),
                )
                .order_by(RecipeBuild.updated_at, RecipeBuild.id)
            ):
                if isinstance(layout, str) and (self._image_cache / layout).is_file():
                    return str(build_id)
        return None

    def convert_upload(self, build_id: str) -> None:
        """Move a Spark build's uploaded archive into the layered store."""

        with self._sessions() as session:
            build = session.get(RecipeBuild, build_id)
            archive_sha256 = None if build is None else build.oci_layout_sha256
        if archive_sha256 is None:
            return
        archive = self._image_cache / archive_sha256
        try:
            image = self._store.import_archive(archive)
        except OciImageStoreError as error:
            _LOGGER.warning(
                "uploaded build %s could not be stored (%s); retrying",
                build_id,
                error.detail,
            )
            return
        with self._sessions.begin() as session:
            build = session.get(RecipeBuild, build_id, with_for_update=True)
            if build is None or build.oci_layout_sha256 != archive_sha256:
                return
            build.image_digest = image.manifest_digest
            build.oci_layout_sha256 = image.manifest_digest.removeprefix("sha256:")
            build.image_bytes = image.stored_bytes
            build.updated_at = self._clock()
        archive.unlink(missing_ok=True)
        _LOGGER.info(
            "stored Spark build %s as runtime image %s",
            build_id,
            image.manifest_digest,
        )

    def _claim(self) -> str | None:
        now = self._clock()
        with self._sessions.begin() as session:
            candidates = session.scalars(
                select(Job)
                .where(
                    Job.kind == PREBUILT_BUILD_KIND,
                    Job.state == "running",
                    # Only Controller pulls; Spark builds are never touched.
                    Job.payload["prebuilt_image"].as_string().is_not(None),
                )
                .order_by(Job.created_at, Job.id)
                .with_for_update(skip_locked=True)
            )
            for job in candidates:
                if prebuilt_reference(job) is None:
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
                    or job.state != "running"
                    or job.payload.get("prebuilt_claim_owner") != self._owner
                ):
                    return
                job.payload = dict(job.payload) | {
                    "prebuilt_claim_until": (self._clock() + _CLAIM_LEASE).isoformat()
                }

    def run(self, job_id: str) -> None:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
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
        except OciImageStoreError as error:
            if error.code != STORE_BUSY:
                self._fail(job_id, build_id, node_id, reference, error)
            else:
                self._release(job_id)
        except OSError as error:
            self._fail(job_id, build_id, node_id, reference, error)
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
        error: OciImageStoreError | OSError,
    ) -> None:
        code = PREBUILT_PULL_FAILED
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
        from .recipe_operations import record_build_evidence

        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update=True)
            build = session.get(RecipeBuild, build_id, with_for_update=True)
            if (
                job is None
                or build is None
                or job.state != "running"
                or job.payload.get("prebuilt_claim_owner") != self._owner
            ):
                return
            payload = {
                key: value
                for key, value in job.payload.items()
                if key not in {"prebuilt_claim_owner", "prebuilt_claim_until"}
            }
            if evidence is not None:
                document = evidence.model_dump(mode="json")
                record_build_evidence(session, build, document, now=now)
                job.state = "succeeded"
                job.result = {
                    "successful_nodes": [node_id],
                    "failed_nodes": [],
                    "node_evidence": {node_id: document},
                }
            else:
                assert failure is not None
                code, detail = failure
                build.state = "failed"
                build.error = f"{code}: {detail}"[:512]
                build.updated_at = now
                job.state = "failed"
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


# A failed pull blocks the same digest for this long; the next plan after it
# tries the pull again, so a registry outage heals without anyone acting.
PREBUILT_RETRY_AFTER = timedelta(hours=1)
PREBUILT_PULL_FAILED = "prebuilt_image_pull_failed"


def prebuilt_failed(
    session: Session,
    recipe_revision_id: str,
    image: PrebuiltImage,
    *,
    now: datetime,
) -> str | None:
    """Why ``image`` is not tried for the revision right now, or ``None``.

    Only a recent failed pull counts. A cancelled build never does: the
    operator stopped it, the image did not fail.
    """

    recent = now - PREBUILT_RETRY_AFTER
    for report, error, updated_at in session.execute(
        select(
            RecipeBuild.policy_report, RecipeBuild.error, RecipeBuild.updated_at
        ).where(
            RecipeBuild.recipe_revision_id == recipe_revision_id,
            RecipeBuild.state == "failed",
        )
    ):
        if (
            policy_prebuilt_reference(report) != image.reference
            or not isinstance(error, str)
            or not error.startswith(PREBUILT_PULL_FAILED)
        ):
            continue
        failed_at = _aware(updated_at, now)
        if failed_at < recent:
            continue
        retry_in = int((failed_at - recent).total_seconds() // 60) + 1
        return (
            f"pulling {image.reference} failed ({error[:200]}); "
            f"it is tried again in {retry_in} min"
        )
    return None


def _aware(value: datetime, reference: datetime) -> datetime:
    # SQLite returns naive timestamps for timezone-aware columns.
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


__all__ = [
    "PREBUILT_KEY_MISMATCH",
    "PREBUILT_KEY_SCHEMA_VERSION",
    "PREBUILT_NOT_PINNED",
    "PREBUILT_PULL_FAILED",
    "PREBUILT_RECENT_PULL_FAILURE",
    "PREBUILT_RETRY_AFTER",
    "PREBUILT_USED",
    "PrebuiltDecision",
    "PrebuiltImage",
    "PrebuiltImageImporter",
    "executable_build_key",
    "package_build_intent",
    "podman_build_arguments",
    "policy_prebuilt_reference",
    "prebuilt_failed",
    "prebuilt_reference",
    "write_library_image_plan",
]

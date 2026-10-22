"""Recipe builds: source concerns."""

from __future__ import annotations

import copy
import io
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import case, select
from vonk_agent_protocol import (
    LifecycleState,
    RecipeBuildCode,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.agent_words import ProfileDocumentState

from ..catalog_revision_contract import RecipeRevisionProjection
from ..categorized_errors import BookkeepingUnknown
from ..content_identity import reusable_build
from ..inventory_items import runtime_value
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import AgentNode, CatalogDocumentRevision, RecipeBuild
from ..operator_receipt_contract import ReceiptOwner
from ..profile_read_observation import observing_profile
from ..recipe_build_receipts import BuildCandidate, PreparedBuildReceipt
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_plan,
    parse_stored_build_policy,
)
from ..source_bundles import (
    GeneratedSourceBundle,
    SourceBundleError,
    SourceBundleIntegrityRefused,
    SourceBundleRefused,
    SourceBundleUnknown,
)
from ..source_policy import (
    SourcePolicyReport,
    dockerfile_base_images,
    inspect_build_source_policy,
)
from .common import (
    _BUILD_OBSERVATION_DELAYS,
    _BUILD_RUNTIME_INTERFACE,
    _BUILD_RUNTIME_PLATFORM,
    _LOGGER,
    _SHA256,
    BUILD_ARTIFACT_FORMAT,
    RecipeBuildRefused,
    RecipeBuildResolution,
    RecipeBuildUnknown,
    _canonical_build,
    _canonical_build_resources,
    _canonical_recipe_document,
    _declared_image_bytes,
    _digest,
    _has_build_receipt,
    _read_recipe_projection,
    _resolved_adapter,
    _source_bundle_handle,
    _source_policy_document,
    derive_build_input_identity,
)

if TYPE_CHECKING:
    from .service import RecipeBuildService


def _verified_bundle(
    self: RecipeBuildService,
    build: Mapping[str, object],
    source_sha256: str,
) -> GeneratedSourceBundle:
    """Read ingress proof; source repair belongs to the worker, never a reader."""
    try:
        bundle = self._bundles.get(source_sha256)
    except SecurityRefusalError:
        raise
    except SourceBundleError as error:
        if observing_profile():
            raise BookkeepingUnknown(
                str(error), reason=WaitReason.RECEIPT_MISSING
            ) from error
        raise RecipeBuildUnknown(error.code, str(error)) from error
    dockerfile = build.get("dockerfile")
    if not isinstance(dockerfile, str) or dockerfile not in bundle.files:
        raise RecipeBuildUnknown(
            RecipeBuildCode.SOURCE_UNAVAILABLE,
            "Verified source bundle does not contain the recipe Dockerfile",
        )
    return bundle


def _heal_source_bundle(
    self: RecipeBuildService,
    projected: RecipeRevisionProjection,
    context_path: object,
    source_sha256: str,
) -> bool:
    if not isinstance(context_path, str):
        return False
    archive = self._source_rederiver(projected, context_path, source_sha256)
    if archive is None:
        return False
    try:
        self._bundles.put(source_sha256, io.BytesIO(archive))
    except SourceBundleUnknown:
        raise
    except (SourceBundleRefused, SourceBundleIntegrityRefused) as error:
        raise RecipeBuildRefused(
            error.code, str(error), reason=error.typed_reason
        ) from error
    except SourceBundleError:
        return False
    _LOGGER.warning("source bundle %s was damaged and was derived again", source_sha256)
    return True


def reconcile_source_bundles(self: RecipeBuildService) -> bool:
    """Restore missing ingress receipts outside requests and SQL transactions.

    Reconstruct from catalog closure and pass the exact digest through normal
    ingress. A changed local copy is a cache miss, never trusted or rehashed.
    The next worker pass retries unavailable closure, without blocking other sources.
    """
    if time.monotonic() < self._source_check_at:
        return False
    self._source_check_at = time.monotonic() + 30.0
    with self._sessions() as session:
        revisions = list(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == ReceiptOwner.RECIPE.value,
                    CatalogDocumentRevision.state == ProfileDocumentState.ACTIVE.value,
                )
            )
        )
    repaired = False
    seen: set[str] = set()
    for revision in revisions:
        try:
            projected = _read_recipe_projection(revision)
            digest = _source_bundle_handle(projected)
            if digest in seen:
                continue
            try:
                self._bundles.verified_manifest(digest)
                seen.add(digest)
                continue
            except SourceBundleUnknown:
                pass
            build = _canonical_build(
                _canonical_recipe_document(revision.document), projected
            )
            context = build.get("context")
            path = context.get("path") if isinstance(context, Mapping) else None
            if self._heal_source_bundle(projected, path, digest):
                seen.add(digest)
                repaired = True
            else:
                _LOGGER.warning(
                    "source %s awaits exact catalog closure; worker retries in 30 seconds",
                    digest,
                )
        except (SourceBundleError, UnknownOutcomeError, RecipeBuildRefused) as error:
            _LOGGER.warning(
                "source verification pending for revision %s: %s", revision.id, error
            )
    return repaired


def check_source(
    self: RecipeBuildService, recipe_revision_id: str
) -> SourcePolicyReport:
    """Re-observe uncertain build facts after releasing each reading session.
    Unknown outcomes are re-observed with a fixed attempt budget and bounded
    backoff and exact inputs. Security/input refusals escape immediately;
    exhaustion preserves the typed cause and releases resources.
    """
    last_error: UnknownOutcomeError | None = None
    for delay in _BUILD_OBSERVATION_DELAYS:
        if delay:
            self._sleep(delay)
        try:
            return self._check_source_once(recipe_revision_id)
        except UnknownOutcomeError as error:
            if observing_profile():
                raise
            last_error = error
    assert last_error is not None
    raise last_error


def _check_source_once(
    self: RecipeBuildService, recipe_revision_id: str
) -> SourcePolicyReport:
    with self._sessions() as session:
        revision = session.get(CatalogDocumentRevision, recipe_revision_id)
        if revision is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.RECIPE_UNRESOLVED,
                "exact recipe authority is unavailable",
            )
        if revision.kind != "recipe" or revision.state != "active":
            raise RecipeBuildUnknown(
                RecipeBuildCode.RECIPE_UNRESOLVED,
                "only a resolved recipe can be checked",
            )
        projected = _read_recipe_projection(revision)
        document = _canonical_recipe_document(revision.document)
        build = _canonical_build(document, projected)
        source_sha256 = _source_bundle_handle(projected)
    bundle = self._verified_bundle(build, source_sha256)
    return inspect_build_source_policy(
        _source_policy_document(document, build, source_sha256), bundle
    )


def resolve(self: RecipeBuildService, recipe_revision_id: str) -> RecipeBuildResolution:
    """Re-observe with the bounded, resource-free policy in ``check_source``."""
    last_error: UnknownOutcomeError | None = None
    for delay in _BUILD_OBSERVATION_DELAYS:
        if delay:
            self._sleep(delay)
        try:
            return self._resolve_once(recipe_revision_id)
        except UnknownOutcomeError as error:
            if observing_profile():
                raise
            last_error = error
    assert last_error is not None
    raise last_error


def _resolve_once(
    self: RecipeBuildService, recipe_revision_id: str
) -> RecipeBuildResolution:
    """Resolve immutable source-build inputs and an exact cached receipt.

    This method intentionally performs no builder lookup, inventory read,
    or capacity admission.  A cache hit is accepted only when the
    current canonical recipe/source policy and executable build inputs
    reproduce the succeeded row's exact final build identity.  A row's
    package handle, notes, or source digest alone is never sufficient.
    """
    with self._sessions() as session:
        revision = session.get(CatalogDocumentRevision, recipe_revision_id)
        if revision is None:
            raise RecipeBuildUnknown(
                RecipeBuildCode.RECIPE_UNRESOLVED,
                "exact recipe authority is unavailable",
            )
        if revision.kind != "recipe" or revision.state != "active":
            raise RecipeBuildUnknown(
                RecipeBuildCode.RECIPE_UNRESOLVED,
                "only a resolved recipe can be built",
            )
        projected = _read_recipe_projection(revision)
        document = _canonical_recipe_document(revision.document)
        build = _canonical_build(document, projected)
        adapter = _resolved_adapter(projected)
        source_sha256 = _source_bundle_handle(projected)

    bundle = self._verified_bundle(build, source_sha256)
    dockerfile_path = build.get("dockerfile")
    dockerfile_payload = (
        bundle.files.get(dockerfile_path) if isinstance(dockerfile_path, str) else None
    )
    if dockerfile_payload is None:
        raise RecipeBuildUnknown(
            RecipeBuildCode.SOURCE_INVALID,
            "recipe Dockerfile authority is unavailable",
        )
    try:
        base_images = list(dockerfile_base_images(dockerfile_payload))
    except ValueError as error:
        raise RecipeBuildUnknown(
            RecipeBuildCode.SOURCE_UNAVAILABLE,
            "exact Dockerfile base authorities are unavailable",
        ) from error
    _canonical_build_resources(projected)
    _declared_image_bytes(document)
    model_inputs = projected.build_model_artifacts
    model_artifacts = (
        model_inputs
        if isinstance(model_inputs, Sequence)
        and not isinstance(model_inputs, (str, bytes))
        else None
    )
    intent = derive_build_input_identity(
        build,
        source_bundle_sha256=source_sha256,
        builder_binary_digest=None,
        artifact_format=BUILD_ARTIFACT_FORMAT,
        base_images=base_images,
        effective_settings=document["settings"],
        model_artifacts=model_artifacts,
        runtime_adapter=adapter.document(),
    )
    intent_sha256 = _digest(intent)
    resolution = RecipeBuildResolution(
        recipe_revision_id=revision.id,
        recipe_content_sha256=revision.content_digest,
        source_bundle_sha256=source_sha256,
        input_intent_sha256=intent_sha256,
        input_intent=copy.deepcopy(intent),
    )

    # Read a bounded snapshot and commit before touching managed storage:
    # a database transaction contains database work only.
    candidates: list[BuildCandidate] = []
    with self._sessions() as session:
        rows = session.scalars(
            select(RecipeBuild)
            .where(
                RecipeBuild.source_bundle_sha256 == source_sha256,
                RecipeBuild.image_digest.is_not(None),
            )
            # A load binds a succeeded row, so one of the same content is
            # preferred over a newer row whose replacement attempt runs or failed.
            .order_by(
                case((RecipeBuild.state == LifecycleState.SUCCEEDED.value, 0), else_=1),
                RecipeBuild.updated_at.desc(),
                RecipeBuild.id.desc(),
            )
        )
        for candidate in rows:
            # A replacement attempt changes execution state, not the last
            # verified artifact. Managed storage below decides whether
            # that exact receipt is still usable.
            if not _has_build_receipt(candidate):
                continue
            try:
                parse_stored_build_plan(candidate.plan)
            except RecipeExecutionContractError:
                continue
            try:
                report = parse_stored_build_policy(candidate.policy_report)
                builder_digest = report.builder_binary_digest
            except RecipeExecutionContractError:
                # Diagnostic policy is disposable. A current builder identity
                # is only a reconstruction candidate: reusable_build below
                # must reproduce the exact accepted executable input digest.
                node = session.get(AgentNode, candidate.builder_node_id)
                builder_digest = (
                    runtime_value(node, lambda identity: identity.binary_digest)
                    if node is not None
                    else None
                )
            if (
                not isinstance(builder_digest, str)
                or _SHA256.fullmatch(builder_digest) is None
            ):
                continue
            if not reusable_build(
                resolution,
                build_input_sha256=candidate.build_input_sha256,
                source_bundle_sha256=source_sha256,
                recorded_builder_binary_digest=builder_digest,
            ):
                continue
            assert candidate.image_digest is not None
            assert candidate.oci_layout_sha256 is not None
            assert candidate.image_bytes is not None
            candidates.append(
                BuildCandidate(
                    build_id=candidate.id,
                    builder_node_id=candidate.builder_node_id,
                    build_input_sha256=candidate.build_input_sha256,
                    builder_binary_digest=builder_digest,
                    image_digest=candidate.image_digest,
                    oci_layout_sha256=candidate.oci_layout_sha256,
                    image_bytes=candidate.image_bytes,
                )
            )

    cached: BuildCandidate | None = None
    prepared: PreparedBuildReceipt | None = None
    receipt_pending = False
    stale_receipt = False
    for candidate in candidates:
        # A verified storage receipt can repair stale SQL size bookkeeping.
        # Look it up by exact executable inputs and archive identity before
        # asking whether the SQL size describes the current stored bytes.
        receipt = None
        if self._prepared_builds is not None:
            receipt = self._prepared_builds(
                candidate.build_input_sha256,
                expected_architecture=_BUILD_RUNTIME_PLATFORM,
                expected_runtime_interface=_BUILD_RUNTIME_INTERFACE,
                expected_archive_sha256=candidate.oci_layout_sha256,
            )
        if not self._stored_archive_present(
            candidate.oci_layout_sha256,
            receipt.image_bytes if receipt is not None else candidate.image_bytes,
        ):
            stale_receipt = True
            continue
        cached = candidate
        prepared = receipt
        receipt_pending = receipt is None
        stale_receipt = False
        break

    if cached is None:
        return replace(resolution, stale_receipt=stale_receipt)
    # The row verified above, never the id a receipt file recorded: catalog
    # collection deletes finished build rows and leaves their receipts.
    build_id = cached.build_id
    if prepared is not None:
        build_input_sha256 = prepared.build_input_sha256
        image_digest = prepared.image_digest
        oci_layout_sha256 = prepared.oci_archive_sha256
        image_bytes = prepared.image_bytes
    else:
        build_input_sha256 = cached.build_input_sha256
        image_digest = cached.image_digest
        oci_layout_sha256 = cached.oci_layout_sha256
        image_bytes = cached.image_bytes
    if (
        not isinstance(build_input_sha256, str)
        or not isinstance(image_digest, str)
        or not isinstance(oci_layout_sha256, str)
        or not isinstance(image_bytes, int)
        or isinstance(image_bytes, bool)
    ):
        # Incomplete receipt evidence is no cache: it is retired as unknown and
        # the resolution reports a stale receipt, so a fresh build replaces it
        # instead of the damaged record blocking the revision.
        retire_as_unknown(
            "recipe-build.receipt",
            str(cached.build_id),
            BookkeepingReason.ROW_INCOMPLETE,
            "cached source build receipt is incomplete",
        )
        return replace(resolution, stale_receipt=True)
    return replace(
        resolution,
        build_input_sha256=build_input_sha256,
        build_id=build_id,
        builder_node_id=cached.builder_node_id,
        builder_binary_digest=cached.builder_binary_digest,
        image_digest=image_digest,
        oci_layout_sha256=oci_layout_sha256,
        image_bytes=image_bytes,
        receipt_pending=receipt_pending,
    )

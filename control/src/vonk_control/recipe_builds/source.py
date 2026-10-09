"""Recipe builds: source concerns."""

from __future__ import annotations

import copy
import io
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import select
from vonk_agent_protocol import RecipeBuildCode, UnknownOutcomeError, WaitReason

from ..catalog_revision_contract import RecipeRevisionProjection
from ..content_identity import reusable_build
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import CatalogDocumentRevision, RecipeBuild
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
    _UNHEALABLE_BUNDLE_CODES,
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
    projected: RecipeRevisionProjection,
    build: Mapping[str, object],
    source_sha256: str,
) -> GeneratedSourceBundle:
    """The stored source bundle, healed from evidence when the stored copy is damaged.

    Source is verified at ingress; what the store holds afterwards can be
    damaged (a lost or corrupted copy, metadata that no longer matches) and
    a damaged stored copy is not a recipe fault. A bundle that cannot be
    read, or that lacks the build's Dockerfile, is derived again from the
    recipe package's closure and stored through the same ingress check (the
    digest must match), then read again. Incomplete local evidence after
    that fresh verification remains unknown; when it cannot be
    derived at all the build remains unknown and the bounded request retry
    observes it again.
    """

    dockerfile = build.get("dockerfile")
    context = build.get("context")
    context_path = context.get("path") if isinstance(context, Mapping) else None

    def read() -> GeneratedSourceBundle | SourceBundleError:
        try:
            return self._bundles.get(source_sha256)
        except SourceBundleUnknown as error:
            if error.code in _UNHEALABLE_BUNDLE_CODES:
                raise
            return error
        except SourceBundleRefused as error:
            raise RecipeBuildRefused(
                error.code, str(error), reason=error.typed_reason
            ) from error
        except SourceBundleError as error:
            return error

    def damaged(value: GeneratedSourceBundle | SourceBundleError) -> bool:
        if isinstance(value, SourceBundleError):
            return value.code not in _UNHEALABLE_BUNDLE_CODES
        return not isinstance(dockerfile, str) or dockerfile not in value.files

    loaded = read()
    if damaged(loaded):
        healed = self._heal_source_bundle(projected, context_path, source_sha256)
        if healed:
            loaded = read()
        if isinstance(loaded, SourceBundleError):
            raise RecipeBuildUnknown(loaded.code, str(loaded)) from loaded
        if damaged(loaded):
            raise RecipeBuildUnknown(
                RecipeBuildCode.SOURCE_UNAVAILABLE,
                "source bundle evidence remains incomplete after rederivation",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
    if isinstance(loaded, SourceBundleError):
        raise RecipeBuildUnknown(loaded.code, str(loaded)) from loaded
    return loaded


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
    bundle = self._verified_bundle(projected, build, source_sha256)
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

    bundle = self._verified_bundle(projected, build, source_sha256)
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
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
        )
        for candidate in rows:
            # A replacement attempt changes execution state, not the last
            # verified artifact. Managed storage below decides whether
            # that exact receipt is still usable.
            if not _has_build_receipt(candidate):
                continue
            try:
                report = parse_stored_build_policy(candidate.policy_report)
                parse_stored_build_plan(candidate.plan)
            except RecipeExecutionContractError:
                continue
            builder_digest = report.builder_binary_digest
            if (
                not isinstance(builder_digest, str)
                or _SHA256.fullmatch(builder_digest) is None
                or report.artifact_format != BUILD_ARTIFACT_FORMAT
                or report.source_bundle_sha256 != source_sha256
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
        # The file on disk, not the SQL row, decides availability. A
        # present archive whose verification receipt is absent or
        # incomplete is a metadata gap that preparation repairs by
        # re-verifying the bytes; only missing bytes are cache loss that
        # forces a rebuild. Trusting the SQL row here would make the row a
        # second availability authority.
        if not self._stored_archive_present(
            candidate.oci_layout_sha256, candidate.image_bytes
        ):
            stale_receipt = True
            continue
        receipt = None
        if self._prepared_builds is not None:
            receipt = self._prepared_builds(
                candidate.build_input_sha256,
                expected_architecture=_BUILD_RUNTIME_PLATFORM,
                expected_runtime_interface=_BUILD_RUNTIME_INTERFACE,
            )
        cached = candidate
        prepared = receipt
        receipt_pending = receipt is None
        stale_receipt = False
        break

    if cached is None:
        return replace(resolution, stale_receipt=stale_receipt)
    if prepared is not None:
        build_id = prepared.build_id or cached.build_id
        build_input_sha256 = prepared.build_input_sha256
        image_digest = prepared.image_digest
        oci_layout_sha256 = prepared.oci_archive_sha256
        image_bytes = prepared.image_bytes
    else:
        build_id = cached.build_id
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

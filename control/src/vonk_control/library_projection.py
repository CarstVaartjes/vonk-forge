"""Bounded read-only Model -> Recipe -> Node Library projection."""

from __future__ import annotations

import copy
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .auth import CursorCodec
from .library_contract import (
    _MAX_AGENT_ROWS,
    _MAX_CANDIDATE_NODES,
    _MAX_EXAMINED_GROUPS,
    _MAX_NODE_ARTIFACTS_PER_NODE,
    _MAX_OPERATIONAL_MEMBERS,
    _MAX_OPERATIONAL_ROWS,
    _MAX_PAGE_RECIPES,
    _MAX_PROJECTED_CAPABILITIES,
    _MAX_RECOMMENDATIONS,
    _MAX_REJECTED_GROUPS,
    _MAX_REJECTED_NODES,
    BuildPreviewInput,
    BuildPreviewTarget,
    FreshnessPolicy,
    ImageDistributionPreviewInput,
    ImageDistributionPreviewTarget,
    InstallPreviewInput,
    InstallPreviewTarget,
    LibraryInstallationSummary,
    LibraryCapabilityFact,
    LibraryCapabilityInventory,
    LibraryCapabilityProvenance,
    LibraryCatalogReference,
    LibraryModel,
    LibraryModelArtifact,
    LibraryModelDefinition,
    LibraryModelFamily,
    LibraryModelFormat,
    LibraryModelLineage,
    LibraryModelLimits,
    LibraryModelMetadata,
    LibraryModelParameters,
    LibraryModelSizes,
    LibraryModelSource,
    LibraryModelVersionFacts,
    ModelVersionIdentity,
    LibraryRecipeDetail,
    LibraryRecipeIdentity,
    LibraryRecipeSummary,
    LibraryRunSummary,
    LibrarySnapshot,
    MappingPreviewInput,
    MappingPreviewTarget,
    OperationalBuild,
    OperationalInstallation,
    OperationalMapping,
    OperationalMappingNode,
    OperationalRun,
    OperationalState,
    PlacementEvidenceCounts,
    PlacementLimits,
    PlacementNode,
    PlacementRecommendation,
    PlacementScore,
    PreviewTarget,
    ProjectionReason,
    RecipeDiskRequirements,
    RecipeFabric,
    RecipeMemoryRequirements,
    RecipeParallelism,
    RecipeRevisionSummary,
    RecipeRole,
    RecipeTopology,
    RejectedNode,
    RunPreviewInput,
    RunPreviewTarget,
    TopologyPlacement,
    VisualArtifact,
    VisualBuild,
    VisualBuildContext,
    VisualCatalogIdentity,
    VisualExecution,
    VisualIdentity,
    VisualInputSlot,
    VisualInterface,
    VisualInterfaceInput,
    VisualInterfaceOutput,
    VisualMetadata,
    VisualModelLicense,
    VisualOutputSlot,
    VisualProvenance,
    VisualRecipeDocument,
    VisualRecipeParameter,
    VisualRuntime,
    VisualTerritorialRestrictions,
    VisualValidation,
    _bounded_reasons,
    _bounded_text,
    _numeric_truncation_count,
    _reason,
    _saturating_headroom,
    _saturating_nonnegative,
    _saturating_nonnegative_sum,
    _utc,
)
from .library_operational import (
    _ACTIVE_RUN_STATES,
    _group_rows,
    _installation_coverage,
    _member_evidence,
    _MemberEvidence,
    _members_are_exact,
    _OperationalRows,
    _PlacementOperationalEvidence,
    _run_health,
    load_placement_operational_evidence,
)
from .models import (
    AgentNode,
    CatalogEntity,
    CatalogEntityRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    NodeArtifact,
    NodeInventorySnapshot,
    NodeTelemetryLatest,
    NodeTelemetrySample,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .catalog_contract import catalog_content_sha256, validate_catalog_document
from .recipe_contract import RecipeContractError, recipe_topology, validate_recipe
from .topology import Placement, TopologyError, validate_topology

_LIBRARY_CURSOR_RESOURCE = "library-recipes"
_LIBRARY_CURSOR_ORDER = "slug-asc/id-asc/v1"


def _cap_operational_rows[Row](
    rows: Sequence[Row],
    collection: str,
    truncations: dict[str, int],
) -> list[Row]:
    """Apply the public row cap after priority ordering and retain evidence."""

    if len(rows) > _MAX_OPERATIONAL_ROWS:
        truncations[collection] = len(rows)
    return list(rows[:_MAX_OPERATIONAL_ROWS])


def _operational_truncation_reason(
    truncations: Mapping[str, int],
) -> ProjectionReason | None:
    if not truncations:
        return None
    ordered_names = ("installations", "runs", "mappings", "builds")
    counts = "; ".join(
        f"{name}: at least {truncations[name]} total/{_MAX_OPERATIONAL_ROWS} returned"
        for name in ordered_names
        if name in truncations
    )
    return _reason(
        "recipe.operational_state_truncated",
        f"Operational state truncated ({counts}); active run lineage was prioritized.",
    )


def _placement_evidence_truncation_reason(
    counts: PlacementEvidenceCounts,
    *,
    severity: Literal["warning", "error"],
) -> ProjectionReason:
    details = []
    for name in counts.truncated_collections:
        limit = (
            _MAX_OPERATIONAL_MEMBERS
            if name.endswith("_members")
            else _MAX_OPERATIONAL_ROWS
        )
        details.append(f"{name}: at least {getattr(counts, name)}/{limit}")
    return _reason(
        "projection.evidence_truncated",
        "Exact current placement evidence exceeded active limits ("
        + "; ".join(details)
        + "); affected groups fail closed.",
        severity,
    )


def _projection_bound_reasons(
    recipe: LocalRecipe,
    document: Mapping[str, object] | None,
    *,
    include_visual_text: bool,
) -> list[ProjectionReason]:
    reasons: list[ProjectionReason] = []
    text_fields_truncated = int(len(recipe.description) > 4_096)
    if document is not None:
        numeric_truncations = _numeric_truncation_count(document)
        if numeric_truncations:
            reasons.append(
                _reason(
                    "recipe.numeric_truncated",
                    f"The bounded Library projection saturated {numeric_truncations} numeric values at the signed-bigint bounds; immutable recipe content is unchanged.",
                )
            )
        if include_visual_text:
            text_fields_truncated += int(
                len(str(document["metadata"]["description"])) > 512
            )
            text_fields_truncated += sum(
                len(str(artifact["repository"])) > 256
                for artifact in document["artifacts"]
            )
    if text_fields_truncated:
        reasons.append(
            _reason(
                "recipe.visual_text_truncated",
                f"The bounded Library projection truncated {text_fields_truncated} text fields; immutable recipe content is unchanged.",
            )
        )
    return reasons


def _revision_summary(value: LocalRecipeRevision) -> RecipeRevisionSummary | None:
    if value.schema_version != 1:
        return None
    return RecipeRevisionSummary(
        id=value.id,
        revision_number=value.revision_number,
        lifecycle=value.lifecycle,
        schema_version=value.schema_version,
        content_sha256=value.content_sha256,
        created_at=_utc(value.created_at),
    )


def _identity(value: LocalRecipe) -> LibraryRecipeIdentity:
    return LibraryRecipeIdentity(
        recipe_id=value.id,
        slug=_bounded_text(value.slug, 128),
        title=_bounded_text(value.title, 200),
        description=_bounded_text(value.description, 4_096),
        source_kind=value.source_kind,
    )


def _validated_document(
    revision: LocalRecipeRevision | None,
) -> tuple[Mapping[str, object] | None, list[ProjectionReason]]:
    if revision is None:
        return None, [
            _reason(
                "recipe.unresolved",
                "The recipe has no stored revision.",
                "error",
            )
        ]
    if revision.schema_version != 1:
        return None, [
            _reason(
                "recipe.schema_version_unsupported",
                "Only recipe schema version 1 can be projected.",
                "error",
            )
        ]
    try:
        validate_recipe(revision.document)
    except (RecipeContractError, TypeError, ValueError) as error:
        return None, [
            _reason(
                "recipe.document_invalid",
                f"The stored recipe document is invalid: {error}",
                "error",
            )
        ]
    reasons: list[ProjectionReason] = []
    if revision.lifecycle != "resolved" or revision.content_sha256 is None:
        reasons.append(
            _reason(
                "recipe.unresolved",
                "This immutable recipe revision is not resolved for operations.",
                "warning",
            )
        )
    return revision.document, reasons


def _topology(value: Mapping[str, object]) -> RecipeTopology:
    roles = []
    for raw_role in value["roles"]:  # type: ignore[index]
        resources = raw_role["resources"]
        roles.append(
            RecipeRole(
                name=_bounded_text(raw_role["name"], 64),
                count=_saturating_nonnegative(raw_role["count"]),
                endpoint_owner=bool(raw_role["endpoint_owner"]),
                artifacts=[_bounded_text(item, 64) for item in raw_role["artifacts"]][
                    :128
                ],
                disk=RecipeDiskRequirements(
                    **{
                        key: _saturating_nonnegative(item)
                        for key, item in resources["disk"].items()
                    }
                ),
                memory=RecipeMemoryRequirements(
                    **{
                        key: (item if key == "kind" else _saturating_nonnegative(item))
                        for key, item in resources["memory"].items()
                    }
                ),
            )
        )
    parallelism = value["parallelism"]
    fabric = value["fabric"]
    return RecipeTopology(
        name=_bounded_text(value["name"], 64),
        mode=_bounded_text(value["mode"], 64),
        node_count=_saturating_nonnegative(value["node_count"]),
        parallelism=RecipeParallelism(
            tensor=_saturating_nonnegative(parallelism["tensor"]),
            pipeline=_saturating_nonnegative(parallelism["pipeline"]),
            data=_saturating_nonnegative(parallelism["data"]),
            backend=_bounded_text(parallelism["backend"], 64),
        ),
        roles=roles,
        fabric=RecipeFabric(
            connectivity=str(fabric["connectivity"]),
            minimum_bandwidth_mbps=_saturating_nonnegative(
                fabric["minimum_bandwidth_mbps"]
            ),
        ),
        start_order=[_bounded_text(item, 64) for item in value["start_order"]][:32],
        stop_order=[_bounded_text(item, 64) for item in value["stop_order"]][:32],
    )


def _catalog_identity(value: Mapping[str, object]) -> VisualCatalogIdentity:
    return VisualCatalogIdentity(
        kind=str(value["kind"]),
        publisher=_bounded_text(value["publisher"], 128),
        slug=_bounded_text(value["slug"], 128),
        content_sha256=str(value["content_sha256"]),
    )


def _visual_parameter(value: Mapping[str, object]) -> VisualRecipeParameter:
    return VisualRecipeParameter(
        name=_bounded_text(value["name"], 64),
        description=_bounded_text(value["description"], 512),
        type=str(value["type"]),
        default=value["default"],
        minimum=None if "minimum" not in value else int(value["minimum"]),
        maximum=None if "maximum" not in value else int(value["maximum"]),
        allowed_values=list(value.get("allowed_values", []))[:128],
        pattern=(
            None if "pattern" not in value else _bounded_text(value["pattern"], 256)
        ),
        change_effect=str(value["change_effect"]),
    )


def _visual_interface_input(
    value: object,
) -> VisualInterfaceInput | None:
    if not isinstance(value, Mapping):
        return None
    slots = [
        VisualInputSlot(
            id=str(slot["id"]),
            label=_bounded_text(slot["label"], 64),
            description=_bounded_text(slot["description"], 256),
            media_types=[
                _bounded_text(media_type, 128) for media_type in slot["media_types"]
            ][:16],
            extensions=[str(extension) for extension in slot["extensions"]][:16],
            min_files=int(slot["min_files"]),
            max_files=int(slot["max_files"]),
            max_file_bytes=int(slot["max_file_bytes"]),
            max_total_bytes=int(slot["max_total_bytes"]),
        )
        for slot in value.get("slots", [])
    ][:32]
    return VisualInterfaceInput(
        path=_bounded_text(value["path"], 512),
        required=bool(value["required"]),
        media_types=[_bounded_text(item, 128) for item in value["media_types"]][:16],
        max_bytes=_saturating_nonnegative(value["max_bytes"]),
        min_files=(
            sum(slot.min_files for slot in slots)
            if slots
            else (1 if value["required"] else 0)
        ),
        max_files=(min(32, sum(slot.max_files for slot in slots)) if slots else 32),
        slots=slots,
    )


def _exact_output_media_types(
    document: Mapping[str, object], interface: Mapping[str, object]
) -> list[str]:
    """Return only MIME types bound literally and checked by this recipe."""

    runtime = document["runtime"]
    validation = document["validation"]
    output_mime = next(
        (
            argument.get("value")
            for argument in runtime["arguments"]
            if argument.get("name") == "output-mime"
        ),
        None,
    )
    if not isinstance(output_mime, str) or "/" not in output_mime:
        return []
    expected_check = "artifact.mime." + output_mime.replace("/", "-")
    checked = any(
        validator["interface"] == interface["adapter"]
        and expected_check in validator["checks"]
        for validator in validation["validators"]
    )
    return [output_mime] if checked else []


def _visual_interface_output(
    document: Mapping[str, object], interface: Mapping[str, object]
) -> VisualInterfaceOutput | None:
    path = interface.get("path")
    if not isinstance(path, str):
        return None
    value = interface.get("output")
    if not isinstance(value, Mapping):
        return VisualInterfaceOutput(
            path=_bounded_text(path, 512),
            allowed_media_types=_exact_output_media_types(document, interface),
        )
    slots = [
        VisualOutputSlot(
            id=str(slot["id"]),
            label=_bounded_text(slot["label"], 64),
            description=_bounded_text(slot["description"], 256),
            media_types=[
                _bounded_text(media_type, 128) for media_type in slot["media_types"]
            ][:16],
            extensions=[str(extension) for extension in slot["extensions"]][:16],
            min_files=int(slot["min_files"]),
            max_files=int(slot["max_files"]),
            max_file_bytes=int(slot["max_file_bytes"]),
            max_total_bytes=int(slot["max_total_bytes"]),
        )
        for slot in value["slots"]
    ][:32]
    media_types = list(
        dict.fromkeys(media_type for slot in slots for media_type in slot.media_types)
    )[:16]
    return VisualInterfaceOutput(
        path=_bounded_text(value["path"], 512),
        allowed_media_types=media_types,
        max_total_bytes=int(value["max_total_bytes"]),
        slots=slots,
    )


def _visual_model_license(
    model_version_document: Mapping[str, object] | None,
) -> VisualModelLicense | None:
    if model_version_document is None:
        return None
    license_document = model_version_document.get("license")
    restriction = (
        license_document.get("territorial_restrictions")
        if isinstance(license_document, Mapping)
        else None
    )
    if not isinstance(restriction, Mapping):
        return VisualModelLicense(territorial_restrictions=None)
    return VisualModelLicense(
        territorial_restrictions=VisualTerritorialRestrictions(
            denied_jurisdictions=[
                str(item) for item in restriction["denied_jurisdictions"]
            ][:32],
            notice=_bounded_text(restriction["notice"], 1_000),
        )
    )


def _resolved_model_version_document(
    session: Session,
    recipe_document: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Load the exact immutable model authority without projecting arbitrary fields."""

    return _resolved_model_version_reference(session, recipe_document["model"])


def _resolved_catalog_revision(
    session: Session,
    reference: Mapping[str, object],
) -> CatalogEntityRevision | None:
    """Load the resolved revision addressed by one content-addressed reference."""

    return session.scalar(
        select(CatalogEntityRevision)
        .join(CatalogEntity, CatalogEntity.id == CatalogEntityRevision.entity_id)
        .where(
            CatalogEntity.kind == reference["kind"],
            CatalogEntity.publisher == reference["publisher"],
            CatalogEntity.slug == reference["slug"],
            CatalogEntityRevision.content_sha256 == reference["content_sha256"],
            CatalogEntityRevision.lifecycle == "resolved",
        )
        .order_by(CatalogEntityRevision.revision_number.desc())
        .limit(1)
    )


def _resolved_model_version_reference(
    session: Session,
    reference: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Load one exact model-version revision addressed by a recipe reference."""

    revision = _resolved_catalog_revision(session, reference)
    return revision.document if revision is not None else None


def _catalog_reference(reference: Mapping[str, object]) -> LibraryCatalogReference:
    return LibraryCatalogReference(
        kind=reference["kind"],
        publisher=reference["publisher"],
        slug=reference["slug"],
        content_sha256=reference["content_sha256"],
    )


def _model_version_facts(
    session: Session,
    reference: Mapping[str, object],
) -> LibraryModelVersionFacts:
    """Project only schema-valid facts from the exact model-version revision.

    The recipe reference is enough to identify a version, but it is not enough
    to claim that version metadata is authoritative.  Invalid, missing, or
    content-mismatched catalog documents therefore remain visible as unknown.
    """

    identity = ModelVersionIdentity(
        kind="model-version",
        publisher=reference["publisher"],
        slug=reference["slug"],
        content_sha256=reference["content_sha256"],
    )
    revision = _resolved_catalog_revision(session, reference)
    if revision is None or not isinstance(revision.document, Mapping):
        return LibraryModelVersionFacts(
            state="unknown",
            identity=identity,
            model=None,
            artifacts=[],
            dependencies=[],
            reasons=[
                _reason(
                    "model.version_metadata_unknown",
                    "The exact resolved model-version document is unavailable.",
                    "info",
                )
            ],
        )
    document = revision.document
    try:
        validate_catalog_document(document)
    except Exception as error:
        return LibraryModelVersionFacts(
            state="unknown",
            identity=identity,
            model=None,
            artifacts=[],
            dependencies=[],
            reasons=[
                _reason(
                    "model.version_metadata_invalid",
                    f"The exact model-version document is not schema-valid: {error}",
                    "warning",
                )
            ],
        )
    if catalog_content_sha256(document) != revision.content_sha256:
        return LibraryModelVersionFacts(
            state="unknown",
            identity=identity,
            model=None,
            artifacts=[],
            dependencies=[],
            reasons=[
                _reason(
                    "model.version_metadata_digest_mismatch",
                    "The resolved model-version document does not match its content digest.",
                    "warning",
                )
            ],
        )
    document_identity = document["identity"]
    if (
        document_identity["publisher"] != reference["publisher"]
        or document_identity["slug"] != reference["slug"]
    ):
        return LibraryModelVersionFacts(
            state="unknown",
            identity=identity,
            model=None,
            artifacts=[],
            dependencies=[],
            reasons=[
                _reason(
                    "model.version_metadata_identity_mismatch",
                    "The resolved model-version document identity does not match its reference.",
                    "warning",
                )
            ],
        )

    model_reference = document["model"]
    reasons: list[ProjectionReason] = []
    model_definition = None
    family = None
    model_revision = _resolved_catalog_revision(session, model_reference)
    if model_revision is not None and isinstance(model_revision.document, Mapping):
        model_document = model_revision.document
        try:
            validate_catalog_document(model_document)
            if catalog_content_sha256(model_document) != model_revision.content_sha256:
                raise ValueError("resolved model document digest mismatch")
            model_definition = LibraryModelDefinition(
                identity=_catalog_reference(model_reference),
                model_group=_catalog_reference(model_document["model_group"]),
                architecture=model_document["architecture"],
                metadata=LibraryModelMetadata(**model_document["metadata"]),
            )
            family_reference = model_document["model_group"]
            family_revision = _resolved_catalog_revision(session, family_reference)
            if family_revision is not None and isinstance(
                family_revision.document, Mapping
            ):
                family_document = family_revision.document
                validate_catalog_document(family_document)
                if catalog_content_sha256(family_document) != family_revision.content_sha256:
                    raise ValueError("resolved model-group document digest mismatch")
                family = LibraryModelFamily(
                    identity=_catalog_reference(family_reference),
                    family=family_document["family"],
                    metadata=LibraryModelMetadata(**family_document["metadata"]),
                )
            else:
                reasons.append(
                    _reason(
                        "model.family_metadata_unknown",
                        "The exact model-group document is unavailable.",
                        "info",
                    )
                )
        except Exception:
            reasons.append(
                _reason(
                    "model.definition_metadata_unknown",
                    "The exact model document is not an accepted schema-valid authority.",
                    "warning",
                )
            )
            model_definition = None
    else:
        reasons.append(
            _reason(
                "model.definition_metadata_unknown",
                "The exact model document is unavailable.",
                "info",
            )
        )

    def catalog_references(values: object) -> list[LibraryCatalogReference]:
        if not isinstance(values, list):
            return []
        return [_catalog_reference(value) for value in values]

    return LibraryModelVersionFacts(
        state="resolved",
        identity=identity,
        model=_catalog_reference(model_reference),
        family=family,
        model_definition=model_definition,
        metadata=LibraryModelMetadata(**document["metadata"]),
        version=document["version"],
        source=LibraryModelSource(**document["source"]),
        lineage=LibraryModelLineage(
            publisher=document["lineage"]["publisher"],
            relation=document["lineage"]["relation"],
            source_model=_catalog_reference(document["lineage"]["source_model"]),
            derivation=document["lineage"]["derivation"],
        ),
        format=LibraryModelFormat(**document["format"]),
        parameters=LibraryModelParameters(**document["parameters"]),
        limits=LibraryModelLimits(**document["limits"]),
        sizes=LibraryModelSizes(**document["sizes"]),
        artifacts=[LibraryModelArtifact(**artifact) for artifact in document["artifacts"]],
        dependencies=catalog_references(document["dependencies"]),
        availability=document["availability"],
        reasons=reasons,
    )


def _capability_digest(value: object) -> str | None:
    if isinstance(value, str) and len(value) == 64:
        try:
            int(value, 16)
        except ValueError:
            return None
        return value
    return None


def _capability_provenance(
    *,
    source_kind: Literal[
        "model-version", "recipe-revision", "model-capability-evidence"
    ],
    publisher: object,
    slug: object,
    content_sha256: object,
    path: object,
    evidence_digest: object,
    revision_id: object = None,
    source_url: object = None,
    source_revision: object = None,
) -> LibraryCapabilityProvenance:
    return LibraryCapabilityProvenance(
        source_kind=source_kind,
        publisher=_bounded_text(publisher, 128),
        slug=_bounded_text(slug, 128),
        content_sha256=_capability_digest(content_sha256),
        path=(None if path is None else _bounded_text(path, 256)),
        evidence_digest=_capability_digest(evidence_digest),
        revision_id=(None if revision_id is None else str(revision_id)),
        source_url=(None if source_url is None else _bounded_text(source_url, 512)),
        source_revision=(
            None if source_revision is None else _bounded_text(source_revision, 80)
        ),
    )


def _capability_inventory(
    *,
    source_kind: Literal["model-version", "recipe-revision"],
    publisher: object,
    slug: object,
    content_sha256: object,
    document: Mapping[str, object] | None,
    raw: object,
    path: str,
    unknown_code: str,
    revision_id: object = None,
) -> LibraryCapabilityInventory:
    """Project explicit capability assertions while preserving unknown state."""

    document_evidence_digest = (
        document.get("capability_evidence_digest") if document is not None else None
    )
    if (
        document_evidence_digest is None
        and isinstance(raw, Mapping)
        and raw.get("evidence_digest") is not None
    ):
        document_evidence_digest = raw.get("evidence_digest")
    provenance = _capability_provenance(
        source_kind=source_kind,
        publisher=publisher,
        slug=slug,
        content_sha256=content_sha256,
        path=path if document is not None else None,
        evidence_digest=document_evidence_digest,
        revision_id=revision_id,
    )
    if document is None or raw is None:
        return LibraryCapabilityInventory(
            state="unknown",
            provenance=provenance,
            reasons=[
                _reason(
                    unknown_code,
                    "No authoritative capability declaration is available for this "
                    "exact identity.",
                    "info",
                )
            ],
        )

    entries: list[tuple[str | None, object, str]] = []
    invalid = 0
    if isinstance(raw, list):
        entries = [(None, item, f"{path}[{index}]") for index, item in enumerate(raw)]
    elif isinstance(raw, Mapping):
        declared = raw.get("declared")
        if isinstance(declared, list):
            entries = [
                (None, item, f"{path}.declared[{index}]")
                for index, item in enumerate(declared)
            ]
        else:
            entries = [
                (str(name), value, f"{path}.{name}")
                for name, value in raw.items()
                if name not in {"evidence_digest", "source", "declared"}
            ]
    else:
        invalid = 1

    facts: list[LibraryCapabilityFact] = []
    for name_hint, value, value_path in entries:
        name: object = name_hint
        support: object = "supported"
        evidence_status: object = "declared"
        evidence_digest: object = document_evidence_digest
        if isinstance(value, str):
            if name_hint is not None and value in {
                "supported",
                "unsupported",
                "unknown",
            }:
                name = name_hint
                support = value
            else:
                name = value if name_hint is None else name_hint
        elif isinstance(value, Mapping):
            name = value.get("name", name_hint)
            support = value.get("support", value.get("status", "supported"))
            evidence_status = value.get(
                "evidence_status", value.get("evidence", "declared")
            )
            evidence_digest = value.get("evidence_digest", evidence_digest)
        elif name_hint is None:
            invalid += 1
            continue
        if not isinstance(name, str) or not name or len(name) > 64:
            invalid += 1
            continue
        if support not in {"supported", "unsupported", "unknown"}:
            support = "unknown"
            invalid += 1
        if evidence_status not in {"declared", "tested", "contradicted", "unknown"}:
            evidence_status = "unknown"
            invalid += 1
        facts.append(
            LibraryCapabilityFact(
                capability=name,
                support=support,
                evidence_status=evidence_status,
                evidence_digest=_capability_digest(evidence_digest),
                provenance=_capability_provenance(
                    source_kind=source_kind,
                    publisher=publisher,
                    slug=slug,
                    content_sha256=content_sha256,
                    path=value_path,
                    evidence_digest=evidence_digest,
                    revision_id=revision_id,
                ),
            )
        )

    # Stable ordering makes JSON comparison and client caching deterministic.
    facts.sort(key=lambda item: (item.capability, item.support, item.evidence_status))
    reasons = []
    if invalid:
        reasons.append(
            _reason(
                "capability.invalid_declaration",
                f"{invalid} capability declaration value(s) were not well formed "
                "and remain unknown.",
                "warning",
            )
        )
    state: Literal["declared", "unknown", "contradictory"] = "declared"
    if not facts and invalid:
        state = "unknown"
    return LibraryCapabilityInventory(
        state=state,
        facts=facts[:_MAX_PROJECTED_CAPABILITIES],
        provenance=provenance,
        reasons=reasons,
    )


_MODEL_CAPABILITY_NAMES = frozenset(
    {
        "chat",
        "text-generation",
        "text-understanding",
        "reasoning",
        "tool-use",
        "code-generation",
        "ocr",
        "image-generation",
        "image-understanding",
        "image-editing",
        "video-generation",
        "video-understanding",
        "audio-generation",
        "audio-understanding",
        "embeddings",
        "3d-generation",
    }
)


def _model_capabilities(
    reference: Mapping[str, object],
    document: Mapping[str, object] | None,
) -> LibraryCapabilityInventory:
    """Project only the accepted schema-2 model capability authority."""

    provenance = _capability_provenance(
        source_kind="model-version",
        publisher=reference.get("publisher"),
        slug=reference.get("slug"),
        content_sha256=reference.get("content_sha256"),
        path="capabilities" if document is not None else None,
        evidence_digest=None,
    )
    raw = document.get("capabilities") if document is not None else None
    if not isinstance(raw, Mapping):
        return LibraryCapabilityInventory(
            state="unknown",
            provenance=provenance,
            reasons=[
                _reason(
                    "model.capabilities_unknown",
                    "The exact model-version has no accepted typed capability declaration.",
                    "info",
                )
            ],
        )
    raw_provenance = raw.get("provenance")
    if (
        not isinstance(raw_provenance, Mapping)
        or not isinstance(raw_provenance.get("source_url"), str)
        or not raw_provenance.get("source_url", "").startswith("https://")
        or not isinstance(raw_provenance.get("source_revision"), str)
        or not isinstance(raw_provenance.get("evidence_digest"), str)
    ):
        return LibraryCapabilityInventory(
            state="unknown",
            provenance=provenance,
            reasons=[
                _reason(
                    "model.capabilities_provenance_invalid",
                    "The capability declaration does not identify the exact model-version.",
                    "warning",
                )
            ],
        )
    facts: list[LibraryCapabilityFact] = []
    invalid = 0
    raw_facts = raw.get("facts")
    for index, raw_fact in enumerate(raw_facts if isinstance(raw_facts, list) else []):
        if not isinstance(raw_fact, Mapping):
            invalid += 1
            continue
        capability = raw_fact.get("capability")
        support = raw_fact.get("support")
        evidence_status = raw_fact.get("evidence_status")
        if capability not in _MODEL_CAPABILITY_NAMES:
            invalid += 1
            continue
        if support not in {"supported", "unsupported", "unknown"}:
            invalid += 1
            support = "unknown"
        if evidence_status not in {"declared", "tested", "contradicted", "unknown"}:
            invalid += 1
            evidence_status = "unknown"
        evidence_digest = _capability_digest(raw_fact.get("evidence_digest"))
        facts.append(
            LibraryCapabilityFact(
                capability=capability,
                support=support,
                evidence_status=evidence_status,
                evidence_digest=evidence_digest,
                provenance=_capability_provenance(
                    source_kind="model-capability-evidence",
                    publisher=reference.get("publisher"),
                    slug=reference.get("slug"),
                    content_sha256=reference.get("content_sha256"),
                    path=f"capabilities.facts[{index}]",
                    evidence_digest=evidence_digest,
                    source_url=raw_provenance.get("source_url"),
                    source_revision=raw_provenance.get("source_revision"),
                ),
            )
        )
    facts.sort(key=lambda item: (item.capability, item.support, item.evidence_status))
    supports: dict[str, set[str]] = {}
    for fact in facts:
        supports.setdefault(fact.capability, set()).add(fact.support)
    contradictory = any(len(values) > 1 for values in supports.values()) or any(
        fact.evidence_status == "contradicted" for fact in facts
    )
    state: Literal["declared", "unknown", "contradictory"] = "declared"
    if contradictory:
        state = "contradictory"
    elif not facts or all(fact.support == "unknown" for fact in facts):
        state = "unknown"
    reasons = []
    if invalid:
        reasons.append(
            _reason(
                "model.capabilities_invalid",
                f"{invalid} model capability fact(s) were invalid and were not projected.",
                "warning",
            )
        )
    if not facts:
        reasons.append(
            _reason(
                "model.capabilities_unknown",
                "The exact model-version declares no usable capability facts.",
                "info",
            )
        )
    return LibraryCapabilityInventory(
        state=state,
        facts=facts[:_MAX_PROJECTED_CAPABILITIES],
        provenance=_capability_provenance(
            source_kind="model-capability-evidence",
            publisher=reference.get("publisher"),
            slug=reference.get("slug"),
            content_sha256=reference.get("content_sha256"),
            path="capabilities.provenance",
            evidence_digest=raw_provenance.get("evidence_digest"),
            source_url=raw_provenance.get("source_url"),
            source_revision=raw_provenance.get("source_revision"),
        ),
        reasons=reasons,
    )


def _recipe_capabilities(
    recipe: LocalRecipe,
    revision: LocalRecipeRevision | None,
    document: Mapping[str, object] | None,
) -> LibraryCapabilityInventory:
    reference_digest = None if revision is None else revision.content_sha256
    raw = None
    if document is not None:
        interfaces = document.get("interfaces")
        if isinstance(interfaces, list):
            raw = [
                {"name": item.get("adapter"), "support": "supported"}
                for item in interfaces
                if isinstance(item, Mapping)
            ]
    inventory = _capability_inventory(
        source_kind="recipe-revision",
        publisher="local-recipe",
        slug=recipe.slug,
        content_sha256=reference_digest,
        document=document,
        raw=raw,
        path="interfaces",
        unknown_code="recipe.capabilities_unknown",
        revision_id=None if revision is None else revision.id,
    )
    return inventory


def _visual_recipe(
    document: Mapping[str, object],
    model_version_document: Mapping[str, object] | None = None,
) -> VisualRecipeDocument:
    identity = document["identity"]
    metadata = document["metadata"]
    build = document["build"]
    context = build["context"]
    network = build["network"]
    build_resources = build["resources"]
    runtime = document["runtime"]
    validation = document["validation"]
    provenance = document["provenance"]
    execution = document["execution"]
    lifecycle = runtime["lifecycle"]
    return VisualRecipeDocument(
        schema_version=1,
        identity=VisualIdentity(
            publisher=_bounded_text(identity["publisher"], 128),
            slug=_bounded_text(identity["slug"], 128),
        ),
        metadata=VisualMetadata(
            title=_bounded_text(metadata["title"], 200),
            description=_bounded_text(metadata["description"], 512),
            tags=[_bounded_text(item, 64) for item in metadata["tags"]][:64],
        ),
        model=_catalog_identity(document["model"]),
        model_license=_visual_model_license(model_version_document),
        execution=VisualExecution(
            harness=_catalog_identity(execution["harness"]),
            patch_bundle=(
                None
                if execution["patch_bundle"] is None
                else _catalog_identity(execution["patch_bundle"])
            ),
        ),
        build=VisualBuild(
            context=VisualBuildContext(
                sha256=str(context["sha256"]),
                expected_bytes=_saturating_nonnegative(context["expected_bytes"]),
                media_type=_bounded_text(context["media_type"], 128),
            ),
            dockerfile=_bounded_text(build["dockerfile"], 256),
            target=(
                None
                if build.get("target") is None
                else _bounded_text(build["target"], 64)
            ),
            platform=_bounded_text(build["platform"], 64),
            network_mode=_bounded_text(network["mode"], 32),
            network_hosts=[_bounded_text(item, 256) for item in network["hosts"]][:64],
            capabilities=[
                _bounded_text(item, 32)
                for item in build["security"]["capabilities"]
            ][:12],
            options={
                **copy.deepcopy(build["options"]),
                "environment": [
                    {"name": item["name"], "value": str(item["value"])}
                    for item in build["options"]["environment"]
                ],
            },
            cpu_cores=int(build_resources["cpu_cores"]),
            download_bytes=_saturating_nonnegative(build_resources["download_bytes"]),
            temporary_bytes=_saturating_nonnegative(build_resources["temporary_bytes"]),
            memory_bytes=_saturating_nonnegative(build_resources["memory_bytes"]),
            processes=int(build_resources["processes"]),
            timeout_seconds=int(build_resources["timeout_seconds"]),
        ),
        parameters=[
            _visual_parameter(parameter) for parameter in document["parameters"]
        ][:128],
        artifacts=[
            VisualArtifact(
                id=_bounded_text(artifact["id"], 64),
                kind=_bounded_text(artifact["kind"], 64),
                repository=_bounded_text(artifact["repository"], 256),
                revision=_bounded_text(artifact["revision"], 128),
                include_paths=sorted(
                    (
                        _bounded_text(item, 512)
                        for item in artifact.get("include_paths", [])
                    ),
                    key=lambda item: item.encode("utf-8"),
                )[:256],
                download_bytes=_saturating_nonnegative(artifact["download_bytes"]),
                installed_bytes=_saturating_nonnegative(artifact["installed_bytes"]),
                roles=[_bounded_text(item, 64) for item in artifact["roles"]][:64],
            )
            for artifact in document["artifacts"]
        ],
        runtime=VisualRuntime(
            distribution=_catalog_identity(runtime["distribution"]),
            entrypoint=[_bounded_text(item, 256) for item in runtime["entrypoint"]][
                :64
            ],
            lifecycle_pre_start_count=len(lifecycle["pre_start"]),
            lifecycle_post_stop_count=len(lifecycle["post_stop"]),
            stop_timeout_seconds=int(lifecycle["stop_timeout_seconds"]),
        ),
        interfaces=[
            VisualInterface(
                adapter=_bounded_text(interface["adapter"], 64),
                port=(None if "port" not in interface else int(interface["port"])),
                model_aliases=[
                    _bounded_text(item, 128) for item in interface["model_aliases"]
                ][:64]
                if "model_aliases" in interface
                else [],
                health_path=(
                    None
                    if "health_path" not in interface
                    else _bounded_text(interface["health_path"], 512)
                ),
                path=(
                    None
                    if "path" not in interface
                    else _bounded_text(interface["path"], 512)
                ),
                timeout_seconds=(
                    int(lifecycle["readiness"]["timeout_seconds"])
                    if "path" in interface and "readiness" in lifecycle
                    else None
                ),
                input=_visual_interface_input(interface.get("input")),
                output=_visual_interface_output(document, interface),
            )
            for interface in document["interfaces"]
        ][:64],
        validation=VisualValidation(
            checks=[
                _bounded_text(check, 80)
                for validator in validation["validators"]
                for check in validator["checks"]
            ][:64],
            benchmark_count=len(validation["benchmarks"]),
        ),
        provenance=VisualProvenance(
            source_kind=str(provenance["source_kind"]),
            source_reference=(
                None
                if provenance["source_reference"] is None
                else _bounded_text(provenance["source_reference"], 2_048)
            ),
            attribution=[
                _bounded_text(item, 512) for item in provenance["attribution"]
            ][:32],
        ),
    )


class _NodeEvidence:
    __slots__ = ("agent", "inventory", "telemetry")

    def __init__(
        self,
        agent: AgentNode,
        inventory: NodeInventorySnapshot,
        telemetry: NodeTelemetrySample,
    ) -> None:
        self.agent = agent
        self.inventory = inventory
        self.telemetry = telemetry


class LibraryProjection:
    """Project recipe rows and advisory placement with a fixed query set."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        cursors: CursorCodec,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        inventory_fresh_seconds: int = 300,
        telemetry_live_seconds: int = 6,
        telemetry_delayed_seconds: int = 20,
        agent_online_seconds: int = 150,
        disk_floor_bytes: int = 10_000_000_000,
        memory_floor_bytes: int = 4_000_000_000,
    ) -> None:
        windows = (
            inventory_fresh_seconds,
            telemetry_live_seconds,
            telemetry_delayed_seconds,
            agent_online_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in windows):
            raise ValueError("Library freshness windows must be positive integers")
        if telemetry_delayed_seconds < telemetry_live_seconds:
            raise ValueError("Library telemetry freshness windows are invalid")
        if disk_floor_bytes < 0 or memory_floor_bytes < 0:
            raise ValueError("Library capacity floors must be nonnegative")
        self._sessions = sessions
        self._cursors = cursors
        self._clock = clock
        self._inventory_fresh_seconds = inventory_fresh_seconds
        self._telemetry_live_seconds = telemetry_live_seconds
        self._telemetry_delayed_seconds = telemetry_delayed_seconds
        self._agent_online_seconds = agent_online_seconds
        self._disk_floor_bytes = disk_floor_bytes
        self._memory_floor_bytes = memory_floor_bytes

    @property
    def _freshness_policy(self) -> FreshnessPolicy:
        return FreshnessPolicy(
            inventory_fresh_seconds=self._inventory_fresh_seconds,
            telemetry_live_seconds=self._telemetry_live_seconds,
            telemetry_delayed_seconds=self._telemetry_delayed_seconds,
        )

    @staticmethod
    def _latest_revision_ids():
        return select(
            LocalRecipeRevision.id.label("revision_id"),
            LocalRecipeRevision.recipe_id.label("recipe_id"),
            func.row_number()
            .over(
                partition_by=LocalRecipeRevision.recipe_id,
                order_by=(
                    LocalRecipeRevision.revision_number.desc(),
                    LocalRecipeRevision.id.desc(),
                ),
            )
            .label("position"),
        ).subquery()

    def list(self, *, limit: int = 100, cursor: str | None = None) -> LibrarySnapshot:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_RECIPES:
            raise ValueError("library limit is invalid")
        context = {"limit": limit}
        boundary: tuple[str, str] | None = None
        if cursor is not None:
            decoded = self._cursors.decode(
                cursor,
                resource=_LIBRARY_CURSOR_RESOURCE,
                order=_LIBRARY_CURSOR_ORDER,
                context=context,
            )
            if (
                not isinstance(decoded, list)
                or len(decoded) != 2
                or not all(isinstance(item, str) for item in decoded)
            ):
                raise ValueError("library cursor is invalid")
            boundary = (decoded[0], decoded[1])
        current = _utc(self._clock())
        latest = self._latest_revision_ids()
        statement = (
            select(LocalRecipe, LocalRecipeRevision)
            .outerjoin(
                latest,
                and_(
                    latest.c.recipe_id == LocalRecipe.id,
                    latest.c.position == 1,
                ),
            )
            .outerjoin(
                LocalRecipeRevision,
                LocalRecipeRevision.id == latest.c.revision_id,
            )
            .order_by(LocalRecipe.slug, LocalRecipe.id)
        )
        if boundary is not None:
            statement = statement.where(
                or_(
                    LocalRecipe.slug > boundary[0],
                    and_(
                        LocalRecipe.slug == boundary[0],
                        LocalRecipe.id > boundary[1],
                    ),
                )
            )
        with self._sessions.begin() as session:
            rows = list(session.execute(statement.limit(limit + 1)))
            page = rows[:limit]
            model_version_facts: dict[
                tuple[str, str, str], LibraryModelVersionFacts
            ] = {}
            model_capability_inventories: dict[
                tuple[str, str, str], LibraryCapabilityInventory
            ] = {}
            recipe_ids = [recipe.id for recipe, _revision in page]
            ranked_installations = (
                select(
                    LocalRecipeRevision.recipe_id.label("recipe_id"),
                    RecipeInstallation.id.label("installation_id"),
                    RecipeInstallation.recipe_revision_id.label("recipe_revision_id"),
                    RecipeInstallation.mapping_id.label("mapping_id"),
                    RecipeInstallation.state.label("state"),
                    RecipeInstallation.mapping_generation.label(
                        "installation_mapping_generation"
                    ),
                    ClusterMapping.state.label("mapping_state"),
                    ClusterMapping.generation.label("mapping_generation"),
                    ClusterMapping.node_count.label("expected_rank_count"),
                    func.count()
                    .over(partition_by=LocalRecipeRevision.recipe_id)
                    .label("total_count"),
                    func.row_number()
                    .over(
                        partition_by=LocalRecipeRevision.recipe_id,
                        order_by=(
                            RecipeInstallation.updated_at.desc(),
                            RecipeInstallation.id.desc(),
                        ),
                    )
                    .label("position"),
                )
                .join(
                    LocalRecipeRevision,
                    LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
                )
                .join(
                    ClusterMapping,
                    ClusterMapping.id == RecipeInstallation.mapping_id,
                )
                .where(
                    LocalRecipeRevision.recipe_id.in_(recipe_ids),
                    RecipeInstallation.state != "uninstalled",
                )
                .subquery()
            )
            installation_rows = list(
                session.execute(
                    select(
                        ranked_installations.c.recipe_id,
                        ranked_installations.c.installation_id,
                        ranked_installations.c.recipe_revision_id,
                        ranked_installations.c.mapping_id,
                        ranked_installations.c.state,
                        ranked_installations.c.installation_mapping_generation,
                        ranked_installations.c.mapping_state,
                        ranked_installations.c.mapping_generation,
                        ranked_installations.c.expected_rank_count,
                        ranked_installations.c.total_count,
                    )
                    .where(ranked_installations.c.position <= 64)
                    .order_by(
                        ranked_installations.c.recipe_id,
                        ranked_installations.c.position,
                    )
                )
            )
            ranked_runs = (
                select(
                    LocalRecipeRevision.recipe_id.label("recipe_id"),
                    RecipeRun.id.label("run_id"),
                    RecipeRun.installation_id.label("installation_id"),
                    RecipeRun.mapping_id.label("mapping_id"),
                    RecipeInstallation.recipe_revision_id.label("recipe_revision_id"),
                    RecipeRun.state.label("state"),
                    RecipeRun.route_state.label("route_state"),
                    RecipeRun.plan.label("plan"),
                    ClusterMapping.node_count.label("expected_rank_count"),
                    func.count()
                    .over(partition_by=LocalRecipeRevision.recipe_id)
                    .label("total_count"),
                    func.row_number()
                    .over(
                        partition_by=LocalRecipeRevision.recipe_id,
                        order_by=(
                            RecipeRun.updated_at.desc(),
                            RecipeRun.id.desc(),
                        ),
                    )
                    .label("position"),
                )
                .join(
                    RecipeInstallation,
                    RecipeInstallation.id == RecipeRun.installation_id,
                )
                .join(
                    LocalRecipeRevision,
                    LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
                )
                .join(ClusterMapping, ClusterMapping.id == RecipeRun.mapping_id)
                .where(
                    LocalRecipeRevision.recipe_id.in_(recipe_ids),
                    RecipeRun.state.in_(_ACTIVE_RUN_STATES),
                )
                .subquery()
            )
            run_rows = list(
                session.execute(
                    select(
                        ranked_runs.c.recipe_id,
                        ranked_runs.c.run_id,
                        ranked_runs.c.installation_id,
                        ranked_runs.c.mapping_id,
                        ranked_runs.c.recipe_revision_id,
                        ranked_runs.c.state,
                        ranked_runs.c.route_state,
                        ranked_runs.c.plan,
                        ranked_runs.c.expected_rank_count,
                        ranked_runs.c.total_count,
                    )
                    .where(ranked_runs.c.position <= 64)
                    .order_by(
                        ranked_runs.c.recipe_id,
                        ranked_runs.c.position,
                    )
                )
            )
            mapping_ids = {
                *(row.mapping_id for row in installation_rows),
                *(row.mapping_id for row in run_rows),
            }
            installation_ids = [row.installation_id for row in installation_rows]
            run_ids = [row.run_id for row in run_rows]
            root_mapping_nodes = list(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id.in_(mapping_ids))
                    .order_by(ClusterMappingNode.mapping_id, ClusterMappingNode.rank)
                    .limit(_MAX_OPERATIONAL_MEMBERS + 1)
                )
            )
            root_installation_nodes = list(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id.in_(installation_ids))
                    .order_by(InstallationNode.installation_id, InstallationNode.rank)
                    .limit(_MAX_OPERATIONAL_MEMBERS + 1)
                )
            )
            root_run_nodes = list(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id.in_(run_ids))
                    .order_by(RunNode.run_id, RunNode.rank)
                    .limit(_MAX_OPERATIONAL_MEMBERS + 1)
                )
            )
        root_members_truncated = any(
            len(items) > _MAX_OPERATIONAL_MEMBERS
            for items in (
                root_mapping_nodes,
                root_installation_nodes,
                root_run_nodes,
            )
        )
        mapping_members = _group_rows(
            root_mapping_nodes[:_MAX_OPERATIONAL_MEMBERS], "mapping_id"
        )
        installation_members = _group_rows(
            root_installation_nodes[:_MAX_OPERATIONAL_MEMBERS], "installation_id"
        )
        run_members = _group_rows(root_run_nodes[:_MAX_OPERATIONAL_MEMBERS], "run_id")
        installations: dict[str, list[LibraryInstallationSummary]] = {}
        installation_totals: dict[str, int] = {}
        for row in installation_rows:
            coverage = _installation_coverage(
                row.state,
                row.mapping_state,
                int(row.mapping_generation),
                int(row.installation_mapping_generation),
                _member_evidence(mapping_members.get(row.mapping_id, [])),
                _member_evidence(installation_members.get(row.installation_id, [])),
                declared_expected_count=int(row.expected_rank_count),
            )
            expected = _saturating_nonnegative(coverage.expected_rank_count)
            installed = _saturating_nonnegative(coverage.installed_rank_count)
            installation_totals[row.recipe_id] = _saturating_nonnegative(
                row.total_count
            )
            installations.setdefault(row.recipe_id, []).append(
                LibraryInstallationSummary(
                    installation_id=row.installation_id,
                    recipe_revision_id=row.recipe_revision_id,
                    state=row.state,
                    installed_rank_count=installed,
                    expected_rank_count=expected,
                    complete=coverage.complete and not root_members_truncated,
                )
            )
        runs: dict[str, list[LibraryRunSummary]] = {}
        run_totals: dict[str, int] = {}
        run_evidence_reasons: dict[str, list[ProjectionReason]] = {}
        for row in run_rows:
            health = _run_health(
                row.plan,
                _member_evidence(run_members.get(row.run_id, [])),
                current=current,
            )
            if health.evidence_code is not None and health.evidence_detail is not None:
                run_evidence_reasons.setdefault(row.recipe_id, []).append(
                    _reason(
                        health.evidence_code,
                        health.evidence_detail,
                        "warning",
                    )
                )
            expected = _saturating_nonnegative(health.expected_rank_count)
            healthy = _saturating_nonnegative(health.healthy_rank_count)
            run_totals[row.recipe_id] = _saturating_nonnegative(row.total_count)
            runs.setdefault(row.recipe_id, []).append(
                LibraryRunSummary(
                    run_id=row.run_id,
                    installation_id=row.installation_id,
                    recipe_revision_id=row.recipe_revision_id,
                    state=row.state,
                    route_state=row.route_state,
                    healthy_rank_count=healthy,
                    expected_rank_count=expected,
                    healthy=health.healthy and not root_members_truncated,
                )
            )
        grouped: dict[tuple[str, str, str], list[LibraryRecipeSummary]] = {}
        unlinked: list[LibraryRecipeSummary] = []
        for recipe, revision in page:
            document, reasons = _validated_document(revision)
            reasons.extend(
                _projection_bound_reasons(
                    recipe,
                    document,
                    include_visual_text=False,
                )
            )
            reasons.extend(run_evidence_reasons.get(recipe.id, []))
            if root_members_truncated:
                reasons.append(
                    _reason(
                        "projection.evidence_truncated",
                        f"Root operational membership evidence exceeded the active limit of {_MAX_OPERATIONAL_MEMBERS}; completeness and health fail closed.",
                        "warning",
                    )
                )
            capabilities: list[str] = []
            topology_name: str | None = None
            model_identity: tuple[str, str, str] | None = None
            model_capabilities = LibraryCapabilityInventory()
            model_version = None
            recipe_capabilities = _recipe_capabilities(
                recipe, revision, document
            )
            if document is not None:
                model = document["model"]
                model_identity = (
                    str(model["publisher"]),
                    str(model["slug"]),
                    str(model["content_sha256"]),
                )
                model_key = (
                    str(model["publisher"]),
                    str(model["slug"]),
                    str(model["content_sha256"]),
                )
                model_version = model_version_facts.get(model_key)
                if model_version is None:
                    model_version = _model_version_facts(session, model)
                    model_version_facts[model_key] = model_version
                model_capabilities = model_capability_inventories.get(model_key)
                if model_capabilities is None:
                    model_capabilities = _model_capabilities(
                        model,
                        (
                            _resolved_model_version_reference(session, model)
                            if model_version.state == "resolved"
                            else None
                        ),
                    )
                    model_capability_inventories[model_key] = model_capabilities
                recipe_capabilities = _recipe_capabilities(recipe, revision, document)
                capabilities = [
                    _bounded_text(item["adapter"], 64)
                    for item in document["interfaces"]
                ][:_MAX_PROJECTED_CAPABILITIES]
                topology_name = _bounded_text(recipe_topology(document)["name"], 64)
            recipe_installations = installations.get(recipe.id, [])
            recipe_runs = runs.get(recipe.id, [])
            installation_total = installation_totals.get(recipe.id, 0)
            run_total = run_totals.get(recipe.id, 0)
            truncated_parts = []
            if installation_total > len(recipe_installations):
                truncated_parts.append(
                    f"installations: {installation_total} total/{len(recipe_installations)} returned"
                )
            if run_total > len(recipe_runs):
                truncated_parts.append(
                    f"runs: {run_total} total/{len(recipe_runs)} returned"
                )
            if truncated_parts:
                reasons.append(
                    _reason(
                        "recipe.operational_summary_truncated",
                        "Bounded current-state summary; "
                        + "; ".join(truncated_parts)
                        + ".",
                    )
                )
            summary = LibraryRecipeSummary(
                **_identity(recipe).model_dump(),
                selected_revision=(
                    None if revision is None else _revision_summary(revision)
                ),
                capabilities=capabilities,
                topology_name=topology_name,
                installations=recipe_installations,
                installation_total_count=installation_total,
                installation_returned_count=len(recipe_installations),
                installations_truncated=(
                    installation_total > len(recipe_installations)
                ),
                runs=recipe_runs,
                run_total_count=run_total,
                run_returned_count=len(recipe_runs),
                runs_truncated=run_total > len(recipe_runs),
                reasons=_bounded_reasons(reasons, 16),
                recipe_capabilities=recipe_capabilities,
            )
            if model_identity is None:
                unlinked.append(summary)
            else:
                grouped.setdefault(model_identity, []).append(summary)
        next_cursor = None
        if len(rows) > limit and page:
            last_recipe = page[-1][0]
            next_cursor = self._cursors.encode(
                resource=_LIBRARY_CURSOR_RESOURCE,
                order=_LIBRARY_CURSOR_ORDER,
                context=context,
                boundary=[last_recipe.slug, last_recipe.id],
            )
        return LibrarySnapshot(
            generated_at=current,
            models=[
                LibraryModel(
                    model={
                        "kind": "model-version",
                        "publisher": publisher,
                        "slug": slug,
                        "content_sha256": content_sha256,
                    },
                    recipes=values,
                    model_capabilities=model_capability_inventories.get(
                        (publisher, slug, content_sha256),
                        LibraryCapabilityInventory(),
                    ),
                    model_version=model_version_facts.get(
                        (publisher, slug, content_sha256)
                    ),
                )
                for (publisher, slug, content_sha256), values in sorted(grouped.items())
            ],
            unlinked_recipes=unlinked,
            next_cursor=next_cursor,
            freshness_policy=self._freshness_policy,
        )

    def detail(self, recipe_id: str) -> LibraryRecipeDetail:
        current = _utc(self._clock())
        latest = self._latest_revision_ids()
        placement_evidence: _PlacementOperationalEvidence | None = None
        model_version_document: Mapping[str, object] | None = None
        model_reference: Mapping[str, object] | None = None
        model_capabilities = LibraryCapabilityInventory()
        recipe_capabilities = LibraryCapabilityInventory()
        model_version = None
        with self._sessions.begin() as session:
            row = session.execute(
                select(LocalRecipe, LocalRecipeRevision)
                .outerjoin(
                    latest,
                    and_(
                        latest.c.recipe_id == LocalRecipe.id,
                        latest.c.position == 1,
                    ),
                )
                .outerjoin(
                    LocalRecipeRevision,
                    LocalRecipeRevision.id == latest.c.revision_id,
                )
                .where(LocalRecipe.id == recipe_id)
            ).one_or_none()
            if row is None:
                raise KeyError(recipe_id)
            recipe, revision = row
            document, reasons = _validated_document(revision)
            if document is not None:
                model_reference = document["model"]
                model_version_document = _resolved_model_version_document(
                    session, document
                )
                model_version = _model_version_facts(session, model_reference)
                model_capabilities = _model_capabilities(
                    model_reference,
                    (
                        model_version_document
                        if model_version.state == "resolved"
                        else None
                    ),
                )
                recipe_capabilities = _recipe_capabilities(recipe, revision, document)
            else:
                recipe_capabilities = _recipe_capabilities(
                    recipe, revision, None
                )
            operational_truncations: dict[str, int] = {}
            # Phase 1: prioritize active runs before the bounded public history.
            run_rows = list(
                session.scalars(
                    select(RecipeRun)
                    .join(
                        RecipeInstallation,
                        RecipeInstallation.id == RecipeRun.installation_id,
                    )
                    .join(
                        LocalRecipeRevision,
                        LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
                    )
                    .where(LocalRecipeRevision.recipe_id == recipe_id)
                    .order_by(
                        case(
                            (RecipeRun.state.in_(_ACTIVE_RUN_STATES), 0),
                            else_=1,
                        ),
                        RecipeRun.updated_at.desc(),
                        RecipeRun.id.desc(),
                    )
                    .limit(_MAX_OPERATIONAL_ROWS + 1)
                )
            )
            runs = _cap_operational_rows(
                run_rows,
                "runs",
                operational_truncations,
            )
            referenced_installation_ids = {item.installation_id for item in runs}
            active_installation_ids = {
                item.installation_id
                for item in runs
                if item.state in _ACTIVE_RUN_STATES
            }
            # Phase 2: retain active-run and complete installed parents before
            # newer partial/history rows consume the public cap.
            installation_rows = list(
                session.scalars(
                    select(RecipeInstallation)
                    .join(
                        LocalRecipeRevision,
                        LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
                    )
                    .where(LocalRecipeRevision.recipe_id == recipe_id)
                    .order_by(
                        case(
                            (
                                RecipeInstallation.id.in_(active_installation_ids),
                                0,
                            ),
                            (RecipeInstallation.state == "installed", 1),
                            (
                                RecipeInstallation.id.in_(referenced_installation_ids),
                                2,
                            ),
                            else_=3,
                        ),
                        RecipeInstallation.updated_at.desc(),
                        RecipeInstallation.id.desc(),
                    )
                    .limit(_MAX_OPERATIONAL_ROWS + 1)
                )
            )
            installations = _cap_operational_rows(
                installation_rows,
                "installations",
                operational_truncations,
            )
            installation_nodes = list(
                session.scalars(
                    select(InstallationNode)
                    .where(
                        InstallationNode.installation_id.in_(
                            [item.id for item in installations]
                        )
                    )
                    .order_by(
                        InstallationNode.installation_id,
                        InstallationNode.rank,
                    )
                    .limit(_MAX_OPERATIONAL_MEMBERS)
                )
            )
            active_mapping_ids = {
                item.mapping_id for item in runs if item.state in _ACTIVE_RUN_STATES
            }
            installed_mapping_ids = {
                item.mapping_id for item in installations if item.state == "installed"
            }
            referenced_mapping_ids = {
                *(item.mapping_id for item in runs),
                *(item.mapping_id for item in installations),
            }
            mapping_rows = list(
                session.scalars(
                    select(ClusterMapping)
                    .join(
                        LocalRecipeRevision,
                        LocalRecipeRevision.id == ClusterMapping.recipe_revision_id,
                    )
                    .where(LocalRecipeRevision.recipe_id == recipe_id)
                    .order_by(
                        case(
                            (ClusterMapping.id.in_(active_mapping_ids), 0),
                            (ClusterMapping.id.in_(installed_mapping_ids), 1),
                            (ClusterMapping.id.in_(referenced_mapping_ids), 2),
                            else_=3,
                        ),
                        ClusterMapping.updated_at.desc(),
                        ClusterMapping.id.desc(),
                    )
                    .limit(_MAX_OPERATIONAL_ROWS + 1)
                )
            )
            mappings = _cap_operational_rows(
                mapping_rows,
                "mappings",
                operational_truncations,
            )
            mapping_nodes = list(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(
                        ClusterMappingNode.mapping_id.in_(
                            [item.id for item in mappings]
                        )
                    )
                    .order_by(ClusterMappingNode.mapping_id, ClusterMappingNode.rank)
                    .limit(_MAX_OPERATIONAL_MEMBERS)
                )
            )
            active_build_ids = {
                item.recipe_build_id
                for item in installations
                if item.id in active_installation_ids
            }
            installed_build_ids = {
                item.recipe_build_id
                for item in installations
                if item.state == "installed"
            }
            referenced_build_ids = {item.recipe_build_id for item in installations}
            build_rows = list(
                session.scalars(
                    select(RecipeBuild)
                    .join(
                        LocalRecipeRevision,
                        LocalRecipeRevision.id == RecipeBuild.recipe_revision_id,
                    )
                    .where(LocalRecipeRevision.recipe_id == recipe_id)
                    .order_by(
                        case(
                            (RecipeBuild.id.in_(active_build_ids), 0),
                            (RecipeBuild.id.in_(installed_build_ids), 1),
                            (RecipeBuild.id.in_(referenced_build_ids), 2),
                            else_=3,
                        ),
                        RecipeBuild.updated_at.desc(),
                        RecipeBuild.id.desc(),
                    )
                    .limit(_MAX_OPERATIONAL_ROWS + 1)
                )
            )
            builds = _cap_operational_rows(
                build_rows,
                "builds",
                operational_truncations,
            )
            run_nodes = list(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id.in_([item.id for item in runs]))
                    .order_by(RunNode.run_id, RunNode.rank)
                    .limit(_MAX_OPERATIONAL_MEMBERS)
                )
            )
            if document is not None and revision is not None:
                placement_evidence = load_placement_operational_evidence(
                    session,
                    revision.id,
                )
            agents = list(
                session.scalars(
                    select(AgentNode)
                    .order_by(AgentNode.node_id)
                    .limit(_MAX_AGENT_ROWS + 1)
                )
            )
            if len(agents) > _MAX_AGENT_ROWS:
                raise ValueError("Library node evidence exceeds 500 nodes")
            node_ids = [item.node_id for item in agents]
            inventories = self._latest_inventory(session, node_ids)
            telemetry = self._latest_telemetry(session, node_ids)
            eligible, rejected, candidate_truncated = self._candidate_nodes(
                agents, inventories, telemetry, current
            )
            candidate_node_ids = [item.agent.node_id for item in eligible]
            active_run_counts = {
                node_id: int(run_count)
                for node_id, run_count in session.execute(
                    select(
                        RunNode.node_id,
                        func.count(RunNode.id),
                    )
                    .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                    .where(
                        RunNode.node_id.in_(candidate_node_ids),
                        RecipeRun.state.in_(_ACTIVE_RUN_STATES),
                    )
                    .group_by(RunNode.node_id)
                    .order_by(RunNode.node_id)
                )
            }
            required_ports = {"29500"}
            if document is not None:
                required_ports.update(
                    str(interface["port"])
                    for interface in document["interfaces"]
                    if "port" in interface
                )
            reservations = self._reservations(
                session, candidate_node_ids, required_ports
            )
            ranked_artifacts = (
                select(
                    NodeArtifact.id.label("artifact_id"),
                    NodeArtifact.node_id.label("node_id"),
                    func.row_number()
                    .over(
                        partition_by=NodeArtifact.node_id,
                        order_by=NodeArtifact.id,
                    )
                    .label("position"),
                    func.count()
                    .over(partition_by=NodeArtifact.node_id)
                    .label("total_count"),
                )
                .where(NodeArtifact.node_id.in_(candidate_node_ids))
                .subquery()
            )
            artifact_rows = list(
                session.execute(
                    select(NodeArtifact, ranked_artifacts.c.total_count)
                    .join(
                        ranked_artifacts,
                        ranked_artifacts.c.artifact_id == NodeArtifact.id,
                    )
                    .where(ranked_artifacts.c.position <= _MAX_NODE_ARTIFACTS_PER_NODE)
                    .order_by(ranked_artifacts.c.node_id, ranked_artifacts.c.position)
                )
            )
            artifacts = [row[0] for row in artifact_rows]
            artifact_truncated_node_ids = {
                row[0].node_id
                for row in artifact_rows
                if int(row.total_count) > _MAX_NODE_ARTIFACTS_PER_NODE
            }
        reasons.extend(
            _projection_bound_reasons(
                recipe,
                document,
                include_visual_text=True,
            )
        )
        truncation_reason = _operational_truncation_reason(operational_truncations)
        if truncation_reason is not None:
            reasons.append(truncation_reason)
        operational_rows = _OperationalRows.collect(
            builds=builds,
            mappings=mappings,
            mapping_nodes=mapping_nodes,
            installations=installations,
            installation_nodes=installation_nodes,
            runs=runs,
            run_nodes=run_nodes,
        )
        operational = self._operational_state(operational_rows)
        if document is None:
            return LibraryRecipeDetail(
                generated_at=current,
                recipe=_identity(recipe),
                selected_revision=(
                    None if revision is None else _revision_summary(revision)
                ),
                visual_recipe=None,
                topology=None,
                operational_state=operational,
                placement=[],
                reasons=_bounded_reasons(reasons, 16),
                model=None,
                model_capabilities=model_capabilities,
                recipe_capabilities=recipe_capabilities,
                model_version=model_version,
            )
        assert revision is not None
        assert placement_evidence is not None
        topology = _topology(recipe_topology(document))
        artifacts_by_node: dict[str, list[NodeArtifact]] = {}
        for artifact in artifacts:
            artifacts_by_node.setdefault(artifact.node_id, []).append(artifact)
        placement = self._place_topology(
            revision,
            document,
            topology,
            eligible,
            rejected,
            candidate_truncated,
            reservations,
            artifacts_by_node,
            artifact_truncated_node_ids,
            active_run_counts,
            placement_evidence,
            current,
        )
        return LibraryRecipeDetail(
            generated_at=current,
            recipe=_identity(recipe),
            selected_revision=_revision_summary(revision),
            visual_recipe=_visual_recipe(document, model_version_document),
            topology=topology,
            operational_state=operational,
            placement=[placement],
            reasons=_bounded_reasons(reasons, 16),
            model=ModelVersionIdentity(
                kind="model-version",
                publisher=str(model_reference["publisher"]),
                slug=str(model_reference["slug"]),
                content_sha256=str(model_reference["content_sha256"]),
            ),
            model_capabilities=model_capabilities,
            recipe_capabilities=recipe_capabilities,
            model_version=model_version,
        )

    @staticmethod
    def _latest_inventory(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, NodeInventorySnapshot]:
        ranked = (
            select(
                NodeInventorySnapshot.id.label("id"),
                func.row_number()
                .over(
                    partition_by=NodeInventorySnapshot.node_id,
                    order_by=(
                        NodeInventorySnapshot.observed_at.desc(),
                        NodeInventorySnapshot.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(NodeInventorySnapshot.node_id.in_(node_ids))
            .subquery()
        )
        rows = session.scalars(
            select(NodeInventorySnapshot)
            .join(ranked, NodeInventorySnapshot.id == ranked.c.id)
            .where(ranked.c.position == 1)
            .order_by(NodeInventorySnapshot.node_id)
        )
        return {row.node_id: row for row in rows}

    @staticmethod
    def _latest_telemetry(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, NodeTelemetrySample]:
        rows = session.scalars(
            select(NodeTelemetrySample)
            .join(
                NodeTelemetryLatest,
                NodeTelemetryLatest.sample_id == NodeTelemetrySample.id,
            )
            .where(NodeTelemetryLatest.node_id.in_(node_ids))
            .order_by(NodeTelemetryLatest.node_id)
        )
        return {row.node_id: row for row in rows}

    @staticmethod
    def _reservations(
        session: Session,
        node_ids: Sequence[str],
        required_port_keys: set[str],
    ) -> dict[str, dict[str, tuple[int, frozenset[str]]]]:
        grouped_key = case(
            (ResourceReservation.kind == "port", ResourceReservation.resource_key),
            else_="",
        )
        rows = session.execute(
            select(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                func.sum(ResourceReservation.amount_bytes),
                grouped_key.label("resource_key"),
            )
            .where(
                ResourceReservation.node_id.in_(node_ids),
                ResourceReservation.state == "active",
                ResourceReservation.kind.in_(
                    {
                        "disk",
                        "unified-memory",
                        "host-memory",
                        "gpu-memory",
                        "port",
                    }
                ),
                or_(
                    ResourceReservation.kind != "port",
                    ResourceReservation.resource_key.in_(required_port_keys),
                ),
            )
            .group_by(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                grouped_key,
            )
            .order_by(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                grouped_key,
            )
        )
        amounts: dict[str, dict[str, int]] = {}
        keys: dict[str, dict[str, set[str]]] = {}
        for node_id, kind, amount, resource_key in rows:
            node_amounts = amounts.setdefault(node_id, {})
            node_amounts[kind] = node_amounts.get(kind, 0) + int(amount)
            keys.setdefault(node_id, {}).setdefault(kind, set()).add(resource_key)
        return {
            node_id: {
                kind: (amount, frozenset(keys[node_id].get(kind, set())))
                for kind, amount in node_amounts.items()
            }
            for node_id, node_amounts in amounts.items()
        }

    def _candidate_nodes(
        self,
        agents: Sequence[AgentNode],
        inventories: Mapping[str, NodeInventorySnapshot],
        telemetry: Mapping[str, NodeTelemetrySample],
        current: datetime,
    ) -> tuple[list[_NodeEvidence], list[RejectedNode], bool]:
        eligible: list[_NodeEvidence] = []
        rejected: list[RejectedNode] = []
        for agent in sorted(agents, key=lambda value: value.node_id):
            reasons: list[ProjectionReason] = []
            if (
                agent.state != "active"
                or agent.revoked_at is not None
                or agent.architecture != "linux-arm64"
            ):
                reasons.append(
                    _reason(
                        "placement.node_incompatible",
                        "The node is inactive, revoked, or not linux-arm64.",
                        "error",
                    )
                )
            last_seen = None if agent.last_seen_at is None else _utc(agent.last_seen_at)
            if (
                last_seen is None
                or current - last_seen < timedelta(0)
                or current - last_seen > timedelta(seconds=self._agent_online_seconds)
            ):
                reasons.append(
                    _reason(
                        "node.offline",
                        "The authenticated agent is not currently online.",
                        "error",
                    )
                )
            inventory = inventories.get(agent.node_id)
            if inventory is None:
                reasons.append(
                    _reason(
                        "inventory.missing",
                        "No authenticated admission inventory is available.",
                        "error",
                    )
                )
            else:
                inventory_age = current - _utc(inventory.observed_at)
                if inventory_age < timedelta(0) or inventory_age > timedelta(
                    seconds=self._inventory_fresh_seconds
                ):
                    reasons.append(
                        _reason(
                            "inventory.stale",
                            "Authenticated admission inventory is stale.",
                            "error",
                        )
                    )
                if inventory.artifact_store_read_only:
                    reasons.append(
                        _reason(
                            "inventory.read_only",
                            "The node artifact store is read-only.",
                            "error",
                        )
                    )
                if "runtime.vonk.v1" not in inventory.capabilities:
                    reasons.append(
                        _reason(
                            "topology.runtime_capability_missing",
                            "The node does not advertise runtime.vonk.v1.",
                            "error",
                        )
                    )
            sample = telemetry.get(agent.node_id)
            if sample is None:
                reasons.append(
                    _reason(
                        "telemetry.missing",
                        "No live telemetry sample is available.",
                        "error",
                    )
                )
            else:
                telemetry_age = current - _utc(sample.observed_at)
                if telemetry_age < timedelta(0) or telemetry_age > timedelta(
                    seconds=self._telemetry_delayed_seconds
                ):
                    reasons.append(
                        _reason(
                            "telemetry.stale",
                            "Telemetry is stale for capacity-sensitive placement.",
                            "error",
                        )
                    )
                elif telemetry_age > timedelta(seconds=self._telemetry_live_seconds):
                    reasons.append(
                        _reason(
                            "telemetry.delayed",
                            "Telemetry is delayed and is not used for placement.",
                            "error",
                        )
                    )
            if reasons:
                if len(rejected) < _MAX_REJECTED_NODES:
                    rejected.append(
                        RejectedNode(
                            node_id=agent.node_id,
                            reasons=_bounded_reasons(reasons, 16),
                        )
                    )
                continue
            assert inventory is not None and sample is not None
            eligible.append(_NodeEvidence(agent, inventory, sample))
        truncated = len(eligible) > _MAX_CANDIDATE_NODES or (
            len(rejected) == _MAX_REJECTED_NODES
            and len(agents) > len(eligible) + len(rejected)
        )
        return eligible[:_MAX_CANDIDATE_NODES], rejected, truncated

    def _operational_state(
        self,
        operational: _OperationalRows,
    ) -> OperationalState:
        installation_by_id = {item.id: item for item in operational.installations}
        return OperationalState(
            builds=[
                OperationalBuild(
                    recipe_build_id=item.id,
                    recipe_revision_id=item.recipe_revision_id,
                    state=item.state,
                    image_digest=item.image_digest,
                    image_bytes=item.image_bytes,
                )
                for item in operational.builds
            ],
            mappings=[
                OperationalMapping(
                    mapping_id=item.id,
                    recipe_revision_id=item.recipe_revision_id,
                    topology_name=item.topology_name,
                    generation=item.generation,
                    state=item.state,
                    nodes=[
                        OperationalMappingNode(
                            node_id=node.node_id,
                            rank=node.rank,
                            role=node.role,
                            endpoint_owner=node.endpoint_owner,
                        )
                        for node in sorted(
                            operational.mapping_members.get(item.id, []),
                            key=lambda value: value.rank,
                        )[:32]
                    ],
                )
                for item in operational.mappings
            ],
            installations=[
                OperationalInstallation(
                    installation_id=item.id,
                    recipe_revision_id=item.recipe_revision_id,
                    mapping_id=item.mapping_id,
                    recipe_build_id=item.recipe_build_id,
                    state=item.state,
                    node_ids=sorted(
                        node.node_id
                        for node in operational.installation_members.get(item.id, [])
                    )[:32],
                )
                for item in operational.installations
            ],
            runs=[
                OperationalRun(
                    run_id=item.id,
                    installation_id=item.installation_id,
                    mapping_id=item.mapping_id,
                    recipe_revision_id=installation_by_id[
                        item.installation_id
                    ].recipe_revision_id,
                    state=item.state,
                    route_state=item.route_state,
                    node_ids=sorted(
                        node.node_id
                        for node in operational.run_members.get(item.id, [])
                    )[:32],
                )
                for item in operational.runs
                if item.installation_id in installation_by_id
            ],
        )

    def _place_topology(
        self,
        revision: LocalRecipeRevision,
        document: Mapping[str, object],
        topology: RecipeTopology,
        candidates: Sequence[_NodeEvidence],
        rejected_nodes: Sequence[RejectedNode],
        candidate_truncated: bool,
        reservations: Mapping[str, Mapping[str, tuple[int, frozenset[str]]]],
        artifacts_by_node: Mapping[str, Sequence[NodeArtifact]],
        artifact_truncated_node_ids: frozenset[str] | set[str],
        active_run_counts: Mapping[str, int],
        operational_evidence: _PlacementOperationalEvidence,
        current: datetime,
    ) -> TopologyPlacement:
        limits = PlacementLimits()
        operational = operational_evidence.operational
        numeric_truncated = _numeric_truncation_count(document) > 0
        if topology.node_count > _MAX_CANDIDATE_NODES:
            unsupported_reasons = [
                _reason(
                    "topology.node_count_unsupported",
                    "This topology requires more than the active 32-node placement limit.",
                    "error",
                )
            ]
            if numeric_truncated:
                unsupported_reasons.append(
                    _reason(
                        "projection.numeric_truncated",
                        "Placement inputs exceed signed-bigint DTO limits; saturated values are not exact evidence.",
                        "warning",
                    )
                )
            if operational_evidence.truncated:
                unsupported_reasons.append(
                    _placement_evidence_truncation_reason(
                        operational_evidence.counts,
                        severity="error",
                    )
                )
            return TopologyPlacement(
                topology_name=topology.name,
                node_count=topology.node_count,
                candidate_node_ids=[],
                recommendations=[],
                rejected_nodes=[],
                rejected_groups=[],
                evaluated_group_count=0,
                search_complete=False,
                rejected_evidence_truncated=False,
                limits=limits,
                evidence_counts=operational_evidence.counts,
                reasons=_bounded_reasons(unsupported_reasons, 16),
            )
        candidate_ids = [item.agent.node_id for item in candidates]
        total_groups = (
            math.comb(len(candidates), topology.node_count)
            if len(candidates) >= topology.node_count
            else 0
        )
        groups = itertools.islice(
            itertools.combinations(candidates, topology.node_count),
            _MAX_EXAMINED_GROUPS,
        )
        recommendations: list[PlacementRecommendation] = []
        rejected_groups: list[PlacementRecommendation] = []
        evaluated = 0
        for group in groups:
            evaluated += 1
            recommendation = self._evaluate_group(
                revision,
                document,
                topology,
                group,
                reservations,
                artifacts_by_node,
                artifact_truncated_node_ids,
                active_run_counts,
                operational,
                operational_evidence.truncated,
                current,
            )
            if recommendation.eligible:
                recommendations.append(recommendation)
            else:
                rejected_groups.append(recommendation)
        recommendations.sort(key=self._recommendation_key)
        rejected_groups.sort(key=lambda value: tuple(value.node_ids))
        artifact_evidence_truncated = bool(
            set(candidate_ids).intersection(artifact_truncated_node_ids)
        )
        search_complete = (
            not candidate_truncated
            and not artifact_evidence_truncated
            and not operational_evidence.truncated
            and total_groups <= _MAX_EXAMINED_GROUPS
        )
        topology_reasons: list[ProjectionReason] = []
        if not search_complete:
            topology_reasons.append(
                _reason(
                    "placement.search_truncated",
                    "Placement ranking is bounded and does not claim exhaustive or global optimality.",
                    "warning",
                )
            )
        if artifact_evidence_truncated:
            topology_reasons.append(
                _reason(
                    "projection.evidence_truncated",
                    f"Artifact evidence exceeded the active per-node limit of {_MAX_NODE_ARTIFACTS_PER_NODE}; affected groups are ineligible.",
                    "warning",
                )
            )
        if operational_evidence.truncated:
            topology_reasons.append(
                _placement_evidence_truncation_reason(
                    operational_evidence.counts,
                    severity="warning",
                )
            )
        if numeric_truncated:
            topology_reasons.append(
                _reason(
                    "projection.numeric_truncated",
                    "Placement inputs exceed signed-bigint DTO limits; saturated values are not exact evidence.",
                    "warning",
                )
            )
        return TopologyPlacement(
            topology_name=topology.name,
            node_count=topology.node_count,
            candidate_node_ids=candidate_ids,
            recommendations=recommendations[:_MAX_RECOMMENDATIONS],
            rejected_nodes=list(rejected_nodes)[:_MAX_REJECTED_NODES],
            rejected_groups=rejected_groups[:_MAX_REJECTED_GROUPS],
            evaluated_group_count=evaluated,
            search_complete=search_complete,
            rejected_evidence_truncated=(
                len(rejected_groups) > _MAX_REJECTED_GROUPS
                or candidate_truncated
                or artifact_evidence_truncated
                or operational_evidence.truncated
            ),
            limits=limits,
            evidence_counts=operational_evidence.counts,
            reasons=_bounded_reasons(topology_reasons, 16),
        )

    @staticmethod
    def _recommendation_key(value: PlacementRecommendation) -> tuple[object, ...]:
        score = value.score
        return (
            not value.eligible,
            not score.exact_install_complete,
            not score.exact_install_partial,
            score.active_run_count,
            -score.artifact_reuse_bytes,
            -score.minimum_disk_headroom_bytes,
            -score.minimum_memory_headroom_bytes,
            score.maximum_telemetry_age_seconds,
            tuple(value.node_ids),
        )

    def _evaluate_group(
        self,
        revision: LocalRecipeRevision,
        document: Mapping[str, object],
        topology: RecipeTopology,
        group: Sequence[_NodeEvidence],
        reservations: Mapping[str, Mapping[str, tuple[int, frozenset[str]]]],
        artifacts_by_node: Mapping[str, Sequence[NodeArtifact]],
        artifact_truncated_node_ids: frozenset[str] | set[str],
        active_run_counts: Mapping[str, int],
        operational: _OperationalRows,
        operational_evidence_truncated: bool,
        current: datetime,
    ) -> PlacementRecommendation:
        ordered = sorted(group, key=lambda value: value.agent.node_id)
        expanded_roles = list(
            itertools.islice(
                itertools.chain.from_iterable(
                    itertools.repeat(role, min(role.count, topology.node_count))
                    for role in topology.roles
                ),
                topology.node_count,
            )
        )
        placements = [
            Placement(item.agent.node_id, rank, role.name, role.endpoint_owner)
            for rank, (item, role) in enumerate(
                zip(ordered, expanded_roles, strict=True)
            )
        ]
        reasons: list[ProjectionReason] = []
        if operational_evidence_truncated:
            reasons.append(
                _reason(
                    "projection.evidence_truncated",
                    "Exact current operational lineage or membership exceeded an active evidence limit; this group fails closed.",
                    "error",
                )
            )
        if any(item.agent.node_id in artifact_truncated_node_ids for item in ordered):
            reasons.append(
                _reason(
                    "projection.evidence_truncated",
                    f"Exact artifact reuse evidence exceeded the active per-node limit of {_MAX_NODE_ARTIFACTS_PER_NODE}; this group fails closed.",
                    "error",
                )
            )
        if _numeric_truncation_count(document):
            reasons.append(
                _reason(
                    "projection.numeric_truncated",
                    "Placement inputs exceed signed-bigint DTO limits; saturated values are not exact evidence.",
                    "warning",
                )
            )
        revision_operable = (
            revision.lifecycle == "resolved" and revision.content_sha256 is not None
        )
        if not revision_operable:
            reasons.append(
                _reason(
                    "mapping.recipe_unresolved",
                    "Only a resolved immutable recipe revision can enter mapping preview.",
                    "error",
                )
            )
        capabilities = {
            item.agent.node_id: tuple(item.inventory.capabilities) for item in ordered
        }
        try:
            validate_topology(document, placements, capabilities)
        except TopologyError as error:
            reasons.append(_reason(error.code, str(error), "error"))
        fabric_addresses = [item.inventory.fabric_address for item in ordered]
        if topology.node_count > 1:
            if any(address is None for address in fabric_addresses):
                reasons.append(
                    _reason(
                        "run.fabric_address_missing",
                        "Authenticated direct-fabric evidence is unavailable.",
                        "error",
                    )
                )
            elif len(set(fabric_addresses)) != len(fabric_addresses):
                reasons.append(
                    _reason(
                        "run.fabric_address_duplicate",
                        "Selected nodes must have unique direct-fabric addresses.",
                        "error",
                    )
                )
            if any(
                item.inventory.fabric_bandwidth_mbps is None
                or item.inventory.fabric_bandwidth_mbps
                < topology.fabric.minimum_bandwidth_mbps
                for item in ordered
            ):
                reasons.append(
                    _reason(
                        "topology.fabric_insufficient",
                        "Authenticated fabric bandwidth is below the topology minimum.",
                        "error",
                    )
                )
        usable_build = None
        if not operational_evidence_truncated:
            usable_build = max(
                (
                    item
                    for item in operational.builds
                    if item.recipe_revision_id == revision.id
                    and item.state == "succeeded"
                    and item.image_digest is not None
                    and item.image_bytes is not None
                ),
                key=lambda item: (_utc(item.updated_at), item.id),
                default=None,
            )
        expected_members = [
            _MemberEvidence(item.node_id, item.rank, item.role) for item in placements
        ]
        active_mapping_ids = {
            item.mapping_id
            for item in operational.runs
            if not operational_evidence_truncated and item.state in _ACTIVE_RUN_STATES
        }
        active_installation_ids = {
            item.installation_id
            for item in operational.runs
            if not operational_evidence_truncated and item.state in _ACTIVE_RUN_STATES
        }
        present_mapping_ids = {
            item.mapping_id
            for item in operational.installations
            if not operational_evidence_truncated and item.state != "uninstalled"
        }
        mapping_by_id = {item.id: item for item in operational.mappings}
        complete_installation_by_id = {
            item.id: _installation_coverage(
                item.state,
                (
                    None
                    if mapping_by_id.get(item.mapping_id) is None
                    else mapping_by_id[item.mapping_id].state
                ),
                (
                    None
                    if mapping_by_id.get(item.mapping_id) is None
                    else mapping_by_id[item.mapping_id].generation
                ),
                item.mapping_generation,
                expected_members,
                _member_evidence(operational.installation_members.get(item.id, [])),
                declared_expected_count=topology.node_count,
            ).complete
            for item in operational.installations
            if not operational_evidence_truncated and item.state != "uninstalled"
        }
        complete_mapping_ids = {
            item.mapping_id
            for item in operational.installations
            if complete_installation_by_id.get(item.id, False)
        }
        persisted_mapping = None
        if not operational_evidence_truncated:
            persisted_mapping = max(
                (
                    item
                    for item in operational.mappings
                    if item.recipe_revision_id == revision.id
                    and item.topology_name == topology.name
                    and item.state == "ready"
                    and _members_are_exact(
                        expected_members,
                        _member_evidence(operational.mapping_members.get(item.id, [])),
                    )
                ),
                key=lambda item: (
                    3
                    if item.id in active_mapping_ids
                    else 2
                    if item.id in complete_mapping_ids
                    else 1
                    if item.id in present_mapping_ids
                    else 0,
                    _utc(item.updated_at),
                    item.id,
                ),
                default=None,
            )
        matching_installations = sorted(
            (
                item
                for item in operational.installations
                if persisted_mapping is not None
                and item.mapping_id == persisted_mapping.id
                and item.state != "uninstalled"
            ),
            key=lambda item: (
                item.id in active_installation_ids,
                complete_installation_by_id.get(item.id, False),
                _utc(item.updated_at),
                item.id,
            ),
            reverse=True,
        )[:16]
        exact_complete = False
        exact_partial = False
        complete_installation_ids: list[str] = []
        for installation in matching_installations:
            complete = complete_installation_by_id[installation.id]
            exact_complete = exact_complete or complete
            exact_partial = exact_partial or not complete
            if complete:
                complete_installation_ids.append(installation.id)
        if operational_evidence_truncated:
            install_state = "unknown"
            reasons.append(
                _reason(
                    "install.evidence_unavailable",
                    "Exact current installation evidence is incomplete; no installation claim is made.",
                    "warning",
                )
            )
        elif exact_complete:
            install_state = "complete"
            reasons.append(
                _reason(
                    "install.complete",
                    "The exact immutable recipe is installed on every rank.",
                    "info",
                )
            )
        elif exact_partial:
            install_state = "partial"
            reasons.append(
                _reason(
                    "install.partial",
                    "The exact installation group is partial or incomplete.",
                    "warning",
                )
            )
        else:
            install_state = "not_present"
            reasons.append(
                _reason(
                    "install.not_present",
                    "The exact immutable recipe is not installed on this group.",
                    "info",
                )
            )
        matching_installation_ids = {item.id for item in matching_installations}
        active_runs = [
            item
            for item in operational.runs
            if item.installation_id in matching_installation_ids
            and item.state in _ACTIVE_RUN_STATES
        ][:16]
        load_state = (
            "unknown"
            if operational_evidence_truncated
            else "loaded"
            if active_runs
            else "not_loaded"
        )
        if active_runs:
            degraded_count = 0
            for run in active_runs:
                health = _run_health(
                    run.plan,
                    _member_evidence(operational.run_members.get(run.id, [])),
                    current=current,
                )
                if not health.healthy:
                    degraded_count += 1
                if (
                    health.evidence_code is not None
                    and health.evidence_detail is not None
                ):
                    reasons.append(
                        _reason(
                            health.evidence_code,
                            health.evidence_detail,
                            "warning",
                        )
                    )
                if run.route_state == "pending":
                    reasons.append(
                        _reason(
                            "run.route_pending",
                            "Route publication is pending. Rank health is projected separately.",
                            "warning",
                        )
                    )
                elif run.route_state == "failed":
                    reasons.append(
                        _reason(
                            "run.route_failed",
                            "Route publication failed. Rank health is projected separately.",
                            "warning",
                        )
                    )
                elif run.route_state == "withdrawn":
                    reasons.append(
                        _reason(
                            "run.route_withdrawn",
                            "Route publication is withdrawn. Rank health is projected separately.",
                            "warning",
                        )
                    )
            if degraded_count:
                reasons.append(
                    _reason(
                        "run.degraded",
                        f"{degraded_count} of {len(active_runs)} active exact runs lacks complete, running, fresh rank evidence.",
                        "warning",
                    )
                )
        group_active_run_count = _saturating_nonnegative_sum(
            *(active_run_counts.get(item.agent.node_id, 0) for item in ordered)
        )
        if operational_evidence_truncated and group_active_run_count:
            reasons.append(
                _reason(
                    "run.presence_unverified",
                    "Active run-rank presence exists, but capped exact lineage prevents a load claim.",
                    "warning",
                )
            )
        elif group_active_run_count:
            detail = (
                f"{group_active_run_count} active run ranks are present on this group, including the selected recipe's exact persisted run."
                if active_runs
                else f"{group_active_run_count} active run ranks from unrelated workloads are present on this group; this does not assert that the selected recipe is loaded."
            )
            reasons.extend(
                [
                    _reason("run.loaded", detail, "info"),
                    _reason(
                        "preference.node_occupied",
                        "Existing active workload evidence makes this group a lower operator-simplicity preference.",
                        "info",
                    ),
                ]
            )
        else:
            reasons.append(
                _reason(
                    "preference.node_empty",
                    "No active workload ranks are present on this candidate group.",
                    "info",
                )
            )
        raw_artifacts = {str(item["id"]): item for item in document["artifacts"]}
        endpoint_port = next(
            (
                int(interface["port"])
                for interface in document["interfaces"]
                if "port" in interface
            ),
            0,
        )
        node_results: list[PlacementNode] = []
        for rank, (evidence, role) in enumerate(
            zip(ordered, expanded_roles, strict=True)
        ):
            node_id = evidence.agent.node_id
            node_reservations = reservations.get(node_id, {})
            disk_reserved = _saturating_nonnegative(
                node_reservations.get("disk", (0, frozenset()))[0]
            )
            if disk_reserved:
                reasons.append(
                    _reason(
                        "reservation.disk",
                        f"{disk_reserved} bytes are actively reserved on {node_id}.",
                        "warning",
                    )
                )
            memory_kind = role.memory.kind
            reservation_kind = {
                "unified": "unified-memory",
                "host": "host-memory",
                "accelerator": "gpu-memory",
            }[memory_kind]
            memory_reserved = _saturating_nonnegative(
                node_reservations.get(reservation_kind, (0, frozenset()))[0]
            )
            if memory_reserved:
                reasons.append(
                    _reason(
                        "reservation.memory",
                        f"{memory_reserved} bytes are actively reserved on {node_id}.",
                        "warning",
                    )
                )
            present = [
                item
                for item in artifacts_by_node.get(node_id, ())
                if item.state == "verified"
            ]
            image_bytes = _saturating_nonnegative(
                usable_build.image_bytes
                if usable_build is not None and usable_build.image_bytes is not None
                else role.disk.image_bytes
            )
            reused_image = 0
            if usable_build is not None and usable_build.image_digest is not None:
                raw_digest = usable_build.image_digest.removeprefix("sha256:")
                if any(
                    item.kind == "image"
                    and item.digest == raw_digest
                    and item.size_bytes == image_bytes
                    for item in present
                ):
                    reused_image = image_bytes
            artifact_bytes = 0
            reused_artifacts = 0
            for artifact_id in role.artifacts:
                artifact = raw_artifacts[artifact_id]
                size = _saturating_nonnegative(artifact["installed_bytes"])
                source = f"{artifact['repository']}@{artifact['revision']}"
                artifact_bytes = _saturating_nonnegative_sum(artifact_bytes, size)
                if any(
                    item.source == source and item.size_bytes == size
                    for item in present
                ):
                    reused_artifacts = _saturating_nonnegative_sum(
                        reused_artifacts, size
                    )
            artifact_reuse = _saturating_nonnegative_sum(reused_image, reused_artifacts)
            disk_required = _saturating_nonnegative_sum(
                image_bytes - reused_image,
                artifact_bytes - reused_artifacts,
                role.disk.staging_bytes,
                role.disk.cache_bytes,
                role.disk.rollback_bytes,
            )
            disk_after = _saturating_headroom(
                evidence.inventory.disk_free_bytes,
                disk_reserved,
                disk_required,
            )
            disk_floor = max(
                _saturating_nonnegative(self._disk_floor_bytes),
                role.disk.safety_margin_bytes,
            )
            if disk_after < disk_floor:
                reasons.append(
                    _reason(
                        "install.insufficient_disk",
                        f"Installation would leave {disk_after} bytes on {node_id}, below the {disk_floor}-byte floor.",
                        "error",
                    )
                )
            memory_available = _saturating_nonnegative(
                min(
                    evidence.inventory.host_memory_free_bytes,
                    evidence.inventory.gpu_memory_free_bytes,
                )
                if memory_kind == "unified"
                else evidence.inventory.host_memory_free_bytes
                if memory_kind == "host"
                else evidence.inventory.gpu_memory_free_bytes
            )
            memory_required = max(
                role.memory.startup_peak_bytes,
                _saturating_nonnegative_sum(
                    role.memory.steady_state_bytes,
                    role.memory.runtime_growth_bytes,
                ),
            )
            memory_floor = max(
                _saturating_nonnegative(self._memory_floor_bytes),
                role.memory.system_reserve_bytes,
            )
            memory_after = _saturating_headroom(
                memory_available,
                memory_reserved,
                memory_required,
            )
            if memory_after < memory_floor:
                reasons.append(
                    _reason(
                        "run.insufficient_memory",
                        f"Run would leave {memory_after} bytes on {node_id}, below the {memory_floor}-byte floor.",
                        "error",
                    )
                )
            ports = node_reservations.get("port", (0, frozenset()))[1]
            if str(endpoint_port) in ports:
                reasons.append(
                    _reason(
                        "run.port_occupied",
                        f"Port {endpoint_port} is already reserved on {node_id}.",
                        "error",
                    )
                )
            if (
                topology.node_count > 1
                and role.endpoint_owner
                and (endpoint_port == 29_500 or "29500" in ports)
            ):
                reasons.append(
                    _reason(
                        "run.rendezvous_port_occupied",
                        "Multi-node rendezvous port 29500 is already reserved.",
                        "error",
                    )
                )
            node_results.append(
                PlacementNode(
                    node_id=node_id,
                    rank=rank,
                    role=role.name,
                    endpoint_owner=role.endpoint_owner,
                    inventory_observed_at=_utc(evidence.inventory.observed_at),
                    telemetry_observed_at=_utc(evidence.telemetry.observed_at),
                    inventory_age_seconds=max(
                        0.0,
                        (
                            current - _utc(evidence.inventory.observed_at)
                        ).total_seconds(),
                    ),
                    telemetry_age_seconds=max(
                        0.0,
                        (
                            current - _utc(evidence.telemetry.observed_at)
                        ).total_seconds(),
                    ),
                    disk_free_bytes=_saturating_nonnegative(
                        evidence.inventory.disk_free_bytes
                    ),
                    disk_reserved_bytes=disk_reserved,
                    disk_required_bytes=disk_required,
                    disk_free_after_bytes=disk_after,
                    memory_kind=memory_kind,
                    memory_available_bytes=memory_available,
                    memory_reserved_bytes=memory_reserved,
                    memory_required_bytes=memory_required,
                    memory_free_after_bytes=memory_after,
                    artifact_reuse_bytes=artifact_reuse,
                    fabric_address=evidence.inventory.fabric_address,
                    fabric_bandwidth_mbps=(
                        None
                        if evidence.inventory.fabric_bandwidth_mbps is None
                        else _saturating_nonnegative(
                            evidence.inventory.fabric_bandwidth_mbps
                        )
                    ),
                )
            )
        reasons.append(
            _reason(
                "action.preview_required",
                "Library ranking is advisory; the existing preview remains authoritative.",
                "info",
            )
        )
        reasons.append(
            _reason(
                "placement.single_group_preview_required",
                "Current desired state does not prove arbitrary co-residency; use the authoritative preview. Existing workloads remain untouched.",
                "warning",
            )
        )
        total_artifact_reuse = _saturating_nonnegative_sum(
            *(item.artifact_reuse_bytes for item in node_results)
        )
        minimum_disk_headroom = min(item.disk_free_after_bytes for item in node_results)
        minimum_memory_headroom = min(
            item.memory_free_after_bytes for item in node_results
        )
        maximum_telemetry_age = max(item.telemetry_age_seconds for item in node_results)
        reasons.extend(
            [
                _reason(
                    "preference.artifact_reuse",
                    f"This group can reuse {total_artifact_reuse} verified artifact bytes.",
                    "info",
                ),
                _reason(
                    "preference.disk_headroom",
                    f"The minimum projected disk headroom is {minimum_disk_headroom} bytes.",
                    "info",
                ),
                _reason(
                    "preference.memory_headroom",
                    f"The minimum projected memory headroom is {minimum_memory_headroom} bytes.",
                    "info",
                ),
                _reason(
                    "preference.telemetry_freshness",
                    f"The oldest candidate telemetry sample is {maximum_telemetry_age:.3f} seconds old.",
                    "info",
                ),
            ]
        )
        eligible = not any(reason.severity == "error" for reason in reasons)
        installation_ids = [item.id for item in matching_installations]
        preview_targets: list[PreviewTarget] = []
        if revision_operable and usable_build is None:
            preview_targets.append(
                BuildPreviewTarget(
                    input=BuildPreviewInput(
                        recipe_revision_id=revision.id,
                        builder_node_id=ordered[0].agent.node_id,
                    )
                )
            )
        if revision_operable:
            preview_targets.append(
                MappingPreviewTarget(
                    input=MappingPreviewInput(
                        recipe_revision_id=revision.id,
                        node_ids=[item.agent.node_id for item in ordered],
                        parameters={},
                    )
                )
            )
        exact_image_present = exact_complete or (
            usable_build is not None
            and usable_build.image_digest is not None
            and usable_build.image_bytes is not None
            and not operational_evidence_truncated
            and not any(
                item.agent.node_id in artifact_truncated_node_ids for item in ordered
            )
            and all(
                any(
                    artifact.kind == "image"
                    and artifact.digest
                    == usable_build.image_digest.removeprefix("sha256:")
                    and artifact.size_bytes == usable_build.image_bytes
                    and artifact.state == "verified"
                    for artifact in artifacts_by_node.get(item.agent.node_id, ())
                )
                for item in ordered
            )
        )
        if (
            revision_operable
            and persisted_mapping is not None
            and usable_build is not None
            and not exact_image_present
        ):
            preview_targets.append(
                ImageDistributionPreviewTarget(
                    input=ImageDistributionPreviewInput(
                        recipe_build_id=usable_build.id,
                        mapping_id=persisted_mapping.id,
                        mapping_generation=persisted_mapping.generation,
                    )
                )
            )
        if (
            revision_operable
            and persisted_mapping is not None
            and usable_build is not None
            and exact_image_present
        ):
            preview_targets.append(
                InstallPreviewTarget(
                    input=InstallPreviewInput(
                        mapping_id=persisted_mapping.id,
                        recipe_build_id=usable_build.id,
                    )
                )
            )
        if (
            revision_operable
            and any(item["adapter"] == "openai" for item in document["interfaces"])
            and complete_installation_ids
        ):
            preview_targets.append(
                RunPreviewTarget(
                    input=RunPreviewInput(installation_id=complete_installation_ids[0])
                )
            )
        score = PlacementScore(
            exact_install_complete=exact_complete,
            exact_install_partial=exact_partial,
            active_run_count=group_active_run_count,
            artifact_reuse_bytes=total_artifact_reuse,
            minimum_disk_headroom_bytes=minimum_disk_headroom,
            minimum_memory_headroom_bytes=minimum_memory_headroom,
            maximum_telemetry_age_seconds=maximum_telemetry_age,
        )
        return PlacementRecommendation(
            recipe_revision_id=revision.id,
            topology_name=topology.name,
            node_ids=[item.agent.node_id for item in ordered],
            nodes=node_results,
            eligible=eligible,
            score=score,
            install_state=install_state,
            load_state=load_state,
            mapping_id=None if persisted_mapping is None else persisted_mapping.id,
            recipe_build_id=None if usable_build is None else usable_build.id,
            installation_ids=installation_ids,
            run_ids=[item.id for item in active_runs],
            preview_targets=preview_targets,
            reasons=_bounded_reasons(reasons, 64),
        )

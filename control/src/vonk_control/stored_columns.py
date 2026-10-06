"""Which contract owns the document in every JSON column of ``models.py``.

``bind`` is the only place a JSON column gets its contract; a column with no
binding fails ``control/tests/test_json_column_contracts.py``.  A table that
stores several document families binds one contract per value of its
``kind`` column.  The mechanism (reading, writing, the write guard and the
structural walker) is :mod:`vonk_control.stored_json`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from vonk_agent_protocol import CacheReferenceReason
from vonk_agent_protocol.inventory import Capability, NetworkInterface
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_agent_protocol.source_bundles import SourceBundleManifest
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .catalog_revision_contract import (
    ModelRevisionProjection,
    RecipeRevisionProjection,
    UnprojectedRevision,
)
from .catalog_sync_contract import ManagedCatalogSyncResult
from .fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileAssignmentInput,
    FleetProfilePreview,
    LabelName,
    LabelValue,
)
from .model_cache_contract import (
    CacheManifest,
    ModelCacheDownloadPayload,
    ModelCacheOperationProgress,
    ModelCacheRemovalPayload,
    ModelCacheRepairPayload,
)
from .recipe_execution_contract import StoredRunEndpoint
from .stored_documents import RouteClaimMarker
from .stored_json import bind

ProfileLabels = Annotated[dict[LabelName, LabelValue], Field(max_length=16)]

bind("jobs", "targets", list[str])
bind("agent_node_profiles", "labels", ProfileLabels)
bind("fleet_profiles", "labels", ProfileLabels)
bind("node_inventory_snapshots", "capabilities", list[Capability])
bind(
    "node_inventory_snapshots",
    "network_interfaces",
    list[NetworkInterface],
    nullable=True,
)
bind("model_cache_sets", "protected_reasons", list[CacheReferenceReason])
bind("run_nodes", "endpoint", StoredRunEndpoint, nullable=True)
bind(
    "route_publications",
    "activation_marker",
    ActivationMarker | RouteClaimMarker,
    nullable=True,
)

bind("fleet_profiles", "assignments", list[FleetProfileAssignmentInput])
bind("fleet_profile_applications", "plan", FleetProfilePreview)
bind("fleet_profile_applications", "progress", FleetProfileApplicationProgress)
bind(
    "fleet_profile_applications",
    "result",
    FleetProfileApplicationResult,
    nullable=True,
)

bind(
    "catalog_document_revisions",
    "document",
    {"model": ModelDefinition, "recipe": RecipeDefinition},
    discriminator="kind",
)
bind(
    "catalog_document_revisions",
    "projected",
    {
        "model": ModelRevisionProjection | UnprojectedRevision,
        "recipe": RecipeRevisionProjection | UnprojectedRevision,
    },
    discriminator="kind",
)
bind("recipe_library_sync_runs", "result", ManagedCatalogSyncResult)
bind("recipe_source_bundles", "manifest", SourceBundleManifest)
bind("model_cache_sets", "manifest", CacheManifest)
bind(
    "model_cache_operations",
    "payload",
    {
        "download": ModelCacheDownloadPayload,
        "repair": ModelCacheRepairPayload,
        "remove": ModelCacheRemovalPayload,
    },
    discriminator="kind",
)
bind(
    "model_cache_operations",
    "progress",
    ModelCacheOperationProgress,
)

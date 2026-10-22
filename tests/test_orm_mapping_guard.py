"""Keep ORM ownership, typed node facts, graph and flush ordering without Git.

Exact outside node columns are frozen pending owner decisions on schema moves,
not exempted by whole table. Operations, authority and configuration cannot
silently acquire unrelated Spark facts either. Removing a column is allowed.
Metadata cannot infer the producer of a scalar on agent_nodes or detect
arbitrary runtime dictionaries; those still require ownership review.
"""

from collections.abc import Mapping
from importlib import import_module
from inspect import getmembers, isclass
from pathlib import Path
from pkgutil import walk_packages

import pytest
from sqlalchemy import JSON, Column, ForeignKey, MetaData, String, Table, inspect
from sqlalchemy.orm import configure_mappers
from sqlalchemy.types import TypeDecorator
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.inventory import HostSettings
from vonk_control import models
from vonk_control.inventory_items_contract import AgentInventoryItems
from vonk_control.stored_json import (
    ContractJSON,
    JsonColumn,
    bindings,
    column_violations,
)

SNAPSHOT = Path(__file__).resolve().parents[1] / "tools/orm-mapping-snapshot.txt"


def test_all_models_share_metadata_and_resolve_foreign_keys() -> None:
    # Discover implementations too: a second Base hidden by the public facade
    # must not escape this guard merely because it has its own mapper registry.
    modules = [models]
    modules.extend(
        import_module(info.name)
        for info in walk_packages(models.__path__, models.__name__ + ".")
    )
    for module in modules:
        for _, model in getmembers(module, isclass):
            mapper = inspect(model, raiseerr=False)
            if mapper is not None and hasattr(mapper, "local_table"):
                assert mapper.registry is models.Base.registry
                assert mapper.local_table.metadata is models.Base.metadata
    for table in models.Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            target = foreign_key.column.table
            assert target is models.Base.metadata.tables[target.key]


def test_mapper_graph_matches_pre_split_snapshot() -> None:
    # SQLAlchemy sorts independent mappers by module + class name during flush.
    # A package move can therefore reorder inserts even with identical tables
    # and no relationships. Pin mapper ordering alongside the relationship set.
    configure_mappers()
    actual = []
    for mapper in models.Base.registry.mappers:
        table = mapper.local_table
        assert isinstance(table, Table)
        relationships = sorted(
            f"{rel.key}:{rel.mapper.class_.__module__}.{rel.mapper.class_.__name__}"
            f":{rel.back_populates}:{rel.direction.name}"
            for rel in mapper.relationships
        )
        actual.append(f"{mapper._sort_key}|{table.name}|" + ",".join(relationships))
    assert sorted(actual) == SNAPSHOT.read_text().splitlines()


FACT_TABLES = frozenset(
    {"agent_nodes", "node_inventory_snapshots", "node_telemetry_samples"}
)
AUDITED_OUTSIDE_COLUMNS: dict[str, frozenset[str]] = {
    # Build operation, provenance and results; builder_node_id is placement.
    "recipe_builds": frozenset(
        [
            "id",
            "recipe_revision_id",
            "builder_node_id",
            "source_bundle_sha256",
            "build_input_sha256",
            "state",
            "policy_report",
            "plan",
            "image_digest",
            "oci_layout_sha256",
            "image_bytes",
            "error",
            "created_at",
            "updated_at",
        ]
    ),
    # Per-installation progress/effects, not general node inventory.
    "installation_nodes": frozenset(
        [
            "id",
            "installation_id",
            "node_id",
            "rank",
            "role",
            "state",
            "required_bytes",
            "installed_bytes",
            "observed_memory_bytes",
            "updated_at",
        ]
    ),
    # Per-run progress/effects and fenced observations, not general node inventory.
    "run_nodes": frozenset(
        [
            "id",
            "run_id",
            "node_id",
            "rank",
            "role",
            "state",
            "port",
            "reserved_memory_bytes",
            "observed_memory_bytes",
            "endpoint",
            "observed_run_generation",
            "observation_process_running",
            "observation_observed_at",
            "observation_endpoint_ready",
            "observation_failure_diagnostics",
            "last_exit_diagnostics",
            "observation_unready_since",
            "updated_at",
        ]
    ),
    # Controller resource promises, not observed free capacity.
    "resource_reservations": frozenset(
        [
            "id",
            "node_id",
            "kind",
            "resource_key",
            "amount_bytes",
            "owner_kind",
            "owner_id",
            "state",
            "plan_digest",
            "created_at",
            "released_at",
        ]
    ),
    # Desired display metadata; hostname is a misplaced agent report.
    "agent_node_profiles": frozenset(
        ["node_id", "display_name", "lifecycle", "labels"]
    ),
    # Certificate authority and issuance/revocation history.
    "agent_certificates": frozenset(
        [
            "wait_evidence",
            "ca_next_attempt_at",
            "serial",
            "node_id",
            "not_before",
            "not_after",
            "fingerprint",
            "state",
            "generation",
            "certificate_pem",
            "chain_pem",
            "csr_public_key_fingerprint",
            "provider_request",
            "csr_pem",
            "revoked_at",
            "ca_revoked_at",
        ]
    ),
    # Certificate rotation intent and issuance workflow.
    "agent_certificate_rotations": frozenset(
        [
            "node_id",
            "source_serial",
            "generation",
            "csr_pem",
            "csr_public_key_fingerprint",
            "provider_request",
            "provider_request_id",
            "state",
            "created_at",
            "updated_at",
        ]
    ),
    # CA revocation recovery, retained independently of enrollment.
    "agent_issued_certificate_revocations": frozenset(
        [
            "wait_evidence",
            "next_attempt_at",
            "serial",
            "node_id",
            "provider_request_id",
            "fingerprint",
            "generation",
            "state",
            "created_at",
            "updated_at",
            "provider_request",
            "csr_pem",
            "ca_revoked_at",
        ]
    ),
    # Operator authorization, not a reported Spark fact.
    "agent_enrollment_grants": frozenset(
        [
            "id",
            "node_id",
            "purpose",
            "requested_display_name",
            "token_digest",
            "created_by",
            "created_at",
            "expires_at",
            "consumed_at",
            "revoked_at",
        ]
    ),
    # Enrollment issuance workflow and immutable bootstrap evidence.
    "agent_enrollments": frozenset(
        [
            "id",
            "grant_id",
            "node_id",
            "provider_request",
            "state",
            "csr_pem",
            "csr_public_key_pem",
            "csr_public_key_fingerprint",
            "host_key_fingerprint",
            "hardware_fingerprint",
            "agent_digest",
            "boot_id",
            "created_at",
            "certificate_pem",
            "chain_pem",
            "certificate_serial",
            "certificate_fingerprint",
            "certificate_generation",
            "certificate_not_before",
            "certificate_not_after",
        ]
    ),
    # Desired workload topology; endpoint_owner_node_id is placement.
    "cluster_mappings": frozenset(
        [
            "id",
            "recipe_revision_id",
            "topology_name",
            "generation",
            "node_count",
            "state",
            "parameters",
            "placement_digest",
            "endpoint_owner_node_id",
            "created_by",
            "created_at",
            "updated_at",
        ]
    ),
    # Desired workload rank placement, not observed node facts.
    "cluster_mapping_nodes": frozenset(
        ["id", "mapping_id", "node_id", "rank", "role", "endpoint_owner", "created_at"]
    ),
    # Authorized operation intent and recovery ownership.
    "agent_operations": frozenset(
        [
            "wait_evidence",
            "next_action_at",
            "repeated_evidence",
            "id",
            "parent_job_id",
            "node_id",
            "kind",
            "payload_digest",
            "payload",
            "authority_revision",
            "workload_intent_ordinal",
            "state",
            "status_reason",
            "current_attempt",
            "observe_count",
            "recovery_deadline",
            "retry_disposition",
            "retry_disposition_attempt",
            "retry_due_at",
            "created_at",
            "updated_at",
        ]
    ),
    # Exact expiring distribution authorization, not artifact presence.
    "artifact_distribution_assignments": frozenset(
        [
            "id",
            "plan_digest",
            "node_id",
            "generation",
            "expires_at",
            "model_artifact_set_sha256",
            "objects",
            "oci_image_digest",
            "oci_image_config_digest",
            "oci_archive_sha256",
            "state",
            "created_at",
            "updated_at",
            "revoked_at",
        ]
    ),
    # Derived sample pointer, not a second copy of telemetry.
    "node_telemetry_latest": frozenset(["node_id", "sample_id"]),
    # Expiring typed event replay; not current-fact authority.
    "fleet_stream_events": frozenset(
        [
            "id",
            "event_type",
            "node_id",
            "entity_kind",
            "entity_id",
            "payload",
            "occurred_at",
            "expires_at",
        ]
    ),
}


def node_fact_violations(
    metadata: MetaData, contracts: Mapping[str, JsonColumn]
) -> list[str]:
    problems: list[str] = []
    for table in metadata.tables.values():
        # Include alternate placement names and FK aliases, not just a literal
        # node_id or tables whose names happen to begin with node_.
        per_node = table.name in FACT_TABLES or any(
            column.name in {"node_id", "spark_id"}
            or column.name.endswith(("_node_id", "_spark_id"))
            or any(
                fk.target_fullname == "agent_nodes.node_id"
                for fk in column.foreign_keys
            )
            for column in table.columns
        )
        if not per_node:
            continue
        for column in table.columns:
            key = f"{table.name}.{column.name}"
            if table.name not in FACT_TABLES and column.name not in (
                AUDITED_OUTSIDE_COLUMNS.get(table.name, frozenset())
            ):
                problems.append(f"{key}: new per-Spark column outside fact owners")
            storage_type = column.type
            if isinstance(storage_type, TypeDecorator):
                storage_type = storage_type.impl
            if isinstance(storage_type, JSON):
                binding = contracts.get(key)
                if binding is None:
                    problems.append(f"{key}: JSON without a typed contract")
                else:
                    problems.extend(
                        f"{key}: {path}: {why}"
                        for path, why in column_violations(binding)
                    )
    # Claims are inventory items. Derive retired scalar identities from their
    # canonical contracts, so adding them back on the contact owner is rejected.
    claimed_fields = set(AgentRuntimeIdentity.model_fields) | set(
        AgentInventoryItems.model_fields
    )
    for name in ("agent_nodes", "agent_node_profiles"):
        table = metadata.tables.get(name)
        if table is not None:
            for field in claimed_fields.intersection(table.columns.keys()):
                problems.append(
                    f"{name}.{field}: agent report belongs in typed inventory"
                )
    return sorted(problems)


def test_per_node_columns_have_one_reviewed_owner_and_typed_json() -> None:
    assert node_fact_violations(models.Base.metadata, bindings()) == []


@pytest.mark.parametrize(
    ("table_name", "identity"),
    [
        ("new_spark_feature", "node_id"),
        ("agent_presence", "node_id"),
        ("new_builder_feature", "builder_node_id"),
        ("new_spark_feature", "spark_id"),
        ("new_contact_feature", "peer"),
    ],
)
def test_guard_rejects_new_side_tables_and_columns_on_existing_ones(
    table_name: str, identity: str
) -> None:
    # Wrong implementation: exempting a whole existing table, only checking
    # primary keys, or requiring the node reference to have one spelling.
    metadata = MetaData()
    Table("agent_nodes", metadata, Column("node_id", String, primary_key=True))
    Table(
        table_name,
        metadata,
        Column(identity, String, ForeignKey("agent_nodes.node_id")),
        Column("invented_fact", String),
    )
    assert any(
        f"{table_name}.invented_fact:" in problem
        for problem in node_fact_violations(metadata, {})
    )


@pytest.mark.parametrize("decorated", [False, True])
def test_guard_rejects_unbound_or_untyped_json_even_on_snapshot_tables(
    decorated: bool,
) -> None:
    # Wrong implementation: treating placement in a snapshot as proof of type,
    # or trusting a binding that still accepts arbitrary controlled structure.
    metadata = MetaData()
    column = (
        Column("probe", ContractJSON[HostSettings]("node_inventory_snapshots", "probe"))
        if decorated
        else Column("probe", JSON)
    )
    Table("node_inventory_snapshots", metadata, column)
    key = "node_inventory_snapshots.probe"
    assert node_fact_violations(metadata, {})
    loose = JsonColumn("node_inventory_snapshots", "probe", {None: dict[str, object]})
    assert node_fact_violations(metadata, {key: loose})
    typed = JsonColumn("node_inventory_snapshots", "probe", {None: HostSettings})
    assert node_fact_violations(metadata, {key: typed}) == []


def test_guard_allows_new_scalar_contact_observations_and_snapshot_items() -> None:
    metadata = MetaData()
    for name in FACT_TABLES:
        Table(name, metadata, Column("probe", String))
    assert node_fact_violations(metadata, {}) == []


@pytest.mark.parametrize("field", sorted(AgentRuntimeIdentity.model_fields))
def test_guard_rejects_runtime_claim_columns_on_contact_owner(field: str) -> None:
    # Wrong implementation: allowing every scalar on agent_nodes after moving
    # its agent claims, so a future feature silently restores the duplicate.
    metadata = MetaData()
    Table(
        "agent_nodes",
        metadata,
        Column("node_id", String, primary_key=True),
        Column(field, String),
    )
    assert any(
        f"agent_nodes.{field}:" in problem
        for problem in node_fact_violations(metadata, {})
    )

"""One-time transactional adoption of retired per-node fact owners."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


def move_contact_facts(connection: Connection) -> None:
    """Copy before dropping; a crash rolls back both effects for startup retry."""
    if "agent_presence" not in inspect(connection).get_table_names():
        return
    connection.execute(
        text("""
        UPDATE agent_nodes AS node
        SET management_address = presence.management_address,
            management_observed_at = presence.observed_at,
            contact_certificate_serial = presence.certificate_serial
        FROM agent_presence AS presence
        WHERE node.node_id = presence.node_id
          AND (node.management_observed_at IS NULL
               OR presence.observed_at > node.management_observed_at)
    """)
    )
    connection.execute(text("DROP TABLE agent_presence"))


def move_runtime_facts(connection: Connection) -> None:
    """Adopt old facts once, then remove their columns in the same transaction."""
    import hashlib
    import uuid
    from datetime import UTC, datetime

    from pydantic import ValidationError
    from sqlalchemy import insert, select
    from vonk_agent_protocol import AgentOperation, LifecycleState, canonical_message
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult

    from ..inventory_items_contract import (
        AgentInventoryItems,
        InventorySnapshotKind,
        PreflightInventoryItem,
    )
    from ..models import NodeInventoryItemsSnapshot
    from .constants import _LOGGER

    inspector = inspect(connection)
    old_columns = {column["name"] for column in inspector.get_columns("agent_nodes")}
    if "architecture" not in old_columns:
        return
    # The replacement uniqueness rule exists before the previous report-only
    # rule is retired. Items and a report may have the same observation time.
    constraints = inspector.get_unique_constraints("node_inventory_snapshots")
    if any(item["name"] == "uq_inventory_node_observed" for item in constraints):
        connection.execute(
            text(
                "ALTER TABLE node_inventory_snapshots DROP CONSTRAINT uq_inventory_node_observed"
            )
        )
    now = datetime.now(UTC)
    results = (
        connection.execute(
            text("""
        SELECT DISTINCT ON (operation.node_id, operation.payload_digest)
            operation.node_id, operation.payload_digest, attempt.result
        FROM agent_operations AS operation
        JOIN agent_operation_attempts AS attempt
          ON attempt.operation_id = operation.id
         AND attempt.attempt = operation.current_attempt
        WHERE operation.kind = :kind AND operation.state = :state
        ORDER BY operation.node_id, operation.payload_digest,
                 operation.updated_at DESC, operation.id DESC
    """),
            {
                "kind": AgentOperation.RUNTIME_PREFLIGHT.value,
                "state": LifecycleState.SUCCEEDED.value,
            },
        )
        .mappings()
        .all()
    )
    preflights: dict[str, list[PreflightInventoryItem]] = {}
    for result in results:
        try:
            entry = PreflightInventoryItem(
                requirements_sha256=result["payload_digest"],
                result=RuntimePreflightResult.model_validate_json(
                    canonical_message(result["result"])
                ),
            )
        except ValidationError:
            # The corrupt receipt remains on its operation. It cannot be made
            # current inventory; normal preflight prepares fresh evidence.
            _LOGGER.warning("Unusable preflight receipt for node %s", result["node_id"])
            continue
        preflights.setdefault(result["node_id"], []).append(entry)
    awaiting_complete_claim = False
    rows = connection.execute(
        text("""
        SELECT node.node_id, node.architecture, node.semantic_version,
            node.build_digest, node.binary_digest, node.preflight_fingerprint,
            node.last_seen_at, profile.hostname
        FROM agent_nodes AS node LEFT JOIN agent_node_profiles AS profile
          ON profile.node_id = node.node_id
    """)
    ).mappings()
    for row in rows:
        identity = None
        if all(
            row[field] is not None
            for field in AgentRuntimeIdentity.model_fields
            if field != "package_activation"
        ):
            identity = AgentRuntimeIdentity.model_validate_json(
                canonical_message(
                    {
                        field: row[field]
                        for field in AgentRuntimeIdentity.model_fields
                        if field != "package_activation"
                    }
                )
            )
        items = AgentInventoryItems(
            runtime_identity=identity,
            hostname=row["hostname"] or None,
            preflight_fingerprint=row["preflight_fingerprint"],
            preflights=preflights.get(row["node_id"], []),
        )
        digest = hashlib.sha256(
            canonical_message({"node_id": row["node_id"], "agent_items": items})
        ).hexdigest()
        existing = connection.scalar(
            select(NodeInventoryItemsSnapshot.id).where(
                NodeInventoryItemsSnapshot.node_id == row["node_id"]
            )
        )
        if existing is None:
            connection.execute(
                insert(NodeInventoryItemsSnapshot).values(
                    id=str(uuid.uuid4()),
                    node_id=row["node_id"],
                    kind=InventorySnapshotKind.ITEMS,
                    observed_at=row["last_seen_at"],
                    received_at=now,
                    evidence_digest=digest,
                    agent_items=items,
                )
            )
        identity_fields = set(AgentRuntimeIdentity.model_fields) - {
            "package_activation"
        }
        if identity is None and any(
            row[field] is not None for field in identity_fields
        ):
            current = connection.scalar(
                select(NodeInventoryItemsSnapshot.agent_items).where(
                    NodeInventoryItemsSnapshot.node_id == row["node_id"]
                )
            )
            if (
                not isinstance(current, AgentInventoryItems)
                or current.runtime_identity is None
            ):
                awaiting_complete_claim = True
                _LOGGER.warning(
                    "Retained incomplete runtime evidence for node %s in retired columns; "
                    "a fresh claim supplies the current identity, and the next startup retires the columns",
                    row["node_id"],
                )
        # This hook is atomic and runs before the new Controller accepts work;
        # if an item already exists after a committed adoption, never overwrite it.
    if awaiting_complete_claim:
        return
    for name in (
        "architecture",
        "semantic_version",
        "build_digest",
        "binary_digest",
        "preflight_fingerprint",
    ):
        connection.execute(text(f"ALTER TABLE agent_nodes DROP COLUMN {name}"))
    connection.execute(text("ALTER TABLE agent_node_profiles DROP COLUMN hostname"))

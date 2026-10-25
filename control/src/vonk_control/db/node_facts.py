"""One-time transactional adoption of retired per-node fact owners."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


def move_contact_facts(connection: Connection) -> None:
    """Give every node its contact row, adopting facts still on agent_nodes.

    Copy before dropping, in startup's one transaction: a crash rolls back
    both effects and the next start repeats the move. A node without a row
    reads as never seen until its next contact, which re-creates the facts.
    """
    from ..models import AgentNodeContact

    # Contact facts lived on agent_nodes until owner-approved lock-reduction
    # step 1 moved them; their names are the contact row's columns.
    old = {column["name"] for column in inspect(connection).get_columns("agent_nodes")}
    retired = sorted(
        old.intersection(AgentNodeContact.__table__.columns.keys()) - {"node_id"}
    )
    names = ", ".join(["node_id", *retired])
    connection.execute(
        text(
            f"INSERT INTO agent_node_contacts ({names}) "
            f"SELECT {names} FROM agent_nodes ON CONFLICT (node_id) DO NOTHING"
        )
    )
    for name in retired:
        connection.execute(text(f"ALTER TABLE agent_nodes DROP COLUMN {name}"))


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
            operation.node_id, operation.payload_digest, attempt.result, operation.updated_at
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
                received_at=result["updated_at"],
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
            contact.last_seen_at, profile.hostname
        FROM agent_nodes AS node LEFT JOIN agent_node_profiles AS profile
          ON profile.node_id = node.node_id
        LEFT JOIN agent_node_contacts AS contact ON contact.node_id = node.node_id
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


def move_artifact_facts(connection: Connection) -> None:
    """Fold availability receipts into the node's typed inventory before retirement."""
    import hashlib
    import uuid
    from datetime import UTC, datetime
    from itertools import groupby

    from sqlalchemy import insert, select, update
    from vonk_agent_protocol import canonical_message

    from ..inventory_items_contract import (
        AgentInventoryItems,
        InventoryArtifactItem,
        InventorySnapshotKind,
    )
    from ..models import NodeInventoryItemsSnapshot

    if "node_artifacts" not in inspect(connection).get_table_names():
        return
    rows = connection.execute(
        text("SELECT * FROM node_artifacts ORDER BY node_id, digest")
    ).mappings()
    now = datetime.now(UTC)
    for node_id, entries in groupby(rows, key=lambda row: row["node_id"]):
        stored = connection.execute(
            select(
                NodeInventoryItemsSnapshot.id,
                NodeInventoryItemsSnapshot.agent_items,
                NodeInventoryItemsSnapshot.observed_at,
            ).where(NodeInventoryItemsSnapshot.node_id == node_id)
        ).one_or_none()
        current = AgentInventoryItems() if stored is None else stored.agent_items
        if not isinstance(current, AgentInventoryItems):
            raise TypeError(
                f"cannot adopt artifacts into unreadable inventory for {node_id}"
            )
        artifacts = {item.digest: item for item in current.artifacts}
        observed = None if stored is None else stored.observed_at
        for row in entries:
            artifact = InventoryArtifactItem.model_validate_json(
                canonical_message(
                    {name: row[name] for name in InventoryArtifactItem.model_fields}
                )
            )
            previous = artifacts.get(artifact.digest)
            if previous is None or previous.updated_at <= artifact.updated_at:
                artifacts[artifact.digest] = artifact
            if observed is None or observed < artifact.updated_at:
                observed = artifact.updated_at
        items = current.model_copy(
            update={
                "artifacts": sorted(artifacts.values(), key=lambda item: item.digest)
            }
        )
        digest = hashlib.sha256(
            canonical_message({"node_id": node_id, "agent_items": items})
        ).hexdigest()
        if stored is None:
            connection.execute(
                insert(NodeInventoryItemsSnapshot).values(
                    id=str(uuid.uuid4()),
                    node_id=node_id,
                    kind=InventorySnapshotKind.ITEMS,
                    observed_at=observed,
                    received_at=now,
                    evidence_digest=digest,
                    agent_items=items,
                )
            )
        else:
            connection.execute(
                update(NodeInventoryItemsSnapshot)
                .where(
                    NodeInventoryItemsSnapshot.id == stored.id,
                )
                .values(agent_items=items, observed_at=observed, evidence_digest=digest)
            )
    connection.execute(text("DROP TABLE node_artifacts"))


def move_repeated_evidence(connection: Connection) -> None:
    """Adopt each summary by its classified owner; never turn commands into contact."""
    from sqlalchemy import select, update
    from vonk_agent_protocol import AgentOperation as OperationKind
    from vonk_agent_protocol import LifecycleState, canonical_message

    from ..evidence_repeats import _utc, load_subject
    from ..models import (
        REPEATED_EVIDENCE_SEQUENCE,
        AgentNodeContact,
        AgentOperation,
        FleetProfileApplication,
    )
    from ..repeated_evidence_contract import RepeatedEvidence, is_contact_subject
    from .constants import _LOGGER

    REPEATED_EVIDENCE_SEQUENCE.create(connection, checkfirst=True)
    if "evidence_repeats" not in inspect(connection).get_table_names():
        return
    # No legacy writer may commit between copying and retiring these rows.
    connection.execute(text("LOCK TABLE evidence_repeats IN ACCESS EXCLUSIVE MODE"))
    rows = (
        connection.execute(
            text("SELECT * FROM evidence_repeats ORDER BY node_id, subject, code")
        )
        .mappings()
        .all()
    )
    profile_prefix = load_subject("")
    for row in rows:
        try:
            evidence = RepeatedEvidence.model_validate_json(
                canonical_message(dict(row))
            )
        except ValueError:
            _LOGGER.warning(
                "Retired repeated evidence is malformed and retained for node %s",
                row["node_id"],
            )
            continue
        operation_owned = False
        if is_contact_subject(evidence.subject):
            target, key, field = (
                AgentNodeContact,
                AgentNodeContact.node_id,
                AgentNodeContact.contact_repeated_evidence,
            )
            statement = select(key.label("identity"), field.label("evidence")).where(
                key == evidence.node_id
            )
        elif evidence.subject.startswith(profile_prefix):
            target, key, field = (
                FleetProfileApplication,
                FleetProfileApplication.id,
                FleetProfileApplication.repeated_evidence,
            )
            statement = select(key.label("identity"), field.label("evidence")).where(
                key == evidence.subject[len(profile_prefix) :]
            )
        else:
            try:
                kind = OperationKind(evidence.subject)
            except ValueError:
                _LOGGER.warning(
                    "Retired repeated evidence has no classified owner: %s",
                    evidence.subject,
                )
                continue
            target, key, field = (
                AgentOperation,
                AgentOperation.id,
                AgentOperation.repeated_evidence,
            )
            operation_owned = True
            statement = (
                select(key.label("identity"), field.label("evidence"))
                .where(
                    AgentOperation.node_id == evidence.node_id,
                    AgentOperation.kind == kind,
                    AgentOperation.created_at <= evidence.last_at,
                )
                .order_by(AgentOperation.updated_at.desc(), AgentOperation.id.desc())
                .limit(1)
            )
        stored = connection.execute(statement).one_or_none()
        if stored is None or (
            stored.evidence is not None and not isinstance(stored.evidence, list)
        ):
            _LOGGER.warning(
                "Retired repeated evidence has no readable owner and remains inert: %s/%s",
                evidence.node_id,
                evidence.subject,
            )
            continue
        previous = stored.evidence or []
        if operation_owned:
            observation_sequence = connection.scalar(
                REPEATED_EVIDENCE_SEQUENCE.next_value()
            )
            assert observation_sequence is not None
            evidence.observation_sequence = observation_sequence
            adopted = [*previous, evidence]
            operation = connection.execute(
                select(AgentOperation.state, AgentOperation.updated_at).where(
                    AgentOperation.id == stored.identity
                )
            ).one()
            if operation.state == LifecycleState.SUCCEEDED and _utc(
                operation.updated_at
            ) >= _utc(evidence.last_at):
                observation_sequence = connection.scalar(
                    REPEATED_EVIDENCE_SEQUENCE.next_value()
                )
                assert observation_sequence is not None
                adopted.append(
                    RepeatedEvidence(
                        node_id=evidence.node_id,
                        subject=evidence.subject,
                        repeats=0,
                        first_at=operation.updated_at,
                        last_at=operation.updated_at,
                        observation_sequence=observation_sequence,
                    )
                )
        else:
            adopted = [
                item
                for item in previous
                if (item.node_id, item.subject, item.code)
                != (evidence.node_id, evidence.subject, evidence.code)
            ] + [evidence]
        connection.execute(
            update(target).where(key == stored.identity).values({field.key: adopted})
        )
        connection.execute(
            text(
                "DELETE FROM evidence_repeats WHERE node_id=:node_identity AND subject=:subject AND code=:code"
            ),
            {
                "node_identity": evidence.node_id,
                "subject": evidence.subject,
                "code": evidence.code,
            },
        )
    if (
        connection.execute(text("SELECT 1 FROM evidence_repeats LIMIT 1")).first()
        is None
    ):
        connection.execute(text("DROP TABLE evidence_repeats"))


def move_resource_facts(connection: Connection) -> None:
    """Adopt each sample's measurements into inventory before retiring columns.

    Receipt identity and the source FK preserve historical observations and
    their retention lifetime. All writes and drops share startup's transaction.
    """
    import hashlib
    import uuid

    from sqlalchemy import insert, select
    from vonk_agent_protocol import canonical_message
    from vonk_agent_protocol.telemetry import HostResourceSample

    from ..inventory_items_contract import InventorySnapshotKind
    from ..models import NodeInventoryResourceSnapshot

    inspector = inspect(connection)
    # Replacement report-only uniqueness was installed by reconciliation.
    for constraint in inspector.get_unique_constraints("node_inventory_snapshots"):
        if constraint["name"] == "uq_inventory_node_kind_observed":
            connection.execute(
                text(
                    "ALTER TABLE node_inventory_snapshots DROP CONSTRAINT uq_inventory_node_kind_observed"
                )
            )
    columns = {
        column["name"] for column in inspector.get_columns("node_telemetry_samples")
    }
    if "memory_total_bytes" not in columns:
        return
    # These rows are protected by the original pair/range constraints. Validate
    # the actual JSON contract too, so invalid input rolls back without loss.
    existing = set(
        connection.scalars(select(NodeInventoryResourceSnapshot.source_sample_id))
    )
    rows = connection.execute(
        text(
            "SELECT id, node_id, observed_at, received_at, memory_total_bytes, memory_available_bytes, disk_total_bytes, disk_free_bytes FROM node_telemetry_samples"
        )
    ).mappings()
    for row in rows:
        if row["id"] in existing:
            continue
        resources = HostResourceSample.model_validate_json(
            canonical_message(
                {name: row[name] for name in HostResourceSample.model_fields}
            )
        )
        connection.execute(
            insert(NodeInventoryResourceSnapshot).values(
                id=str(uuid.uuid4()),
                node_id=row["node_id"],
                kind=InventorySnapshotKind.MEASUREMENTS,
                observed_at=row["observed_at"],
                received_at=row["received_at"],
                source_sample_id=row["id"],
                host_resources=resources,
                evidence_digest=hashlib.sha256(
                    canonical_message(
                        {
                            "node_id": row["node_id"],
                            "source_sample_id": row["id"],
                            "host_resources": resources,
                        }
                    )
                ).hexdigest(),
            )
        )
    for name in HostResourceSample.model_fields:
        # Field identifiers come only from the canonical contract.
        connection.execute(
            text(f'ALTER TABLE node_telemetry_samples DROP COLUMN "{name}"')
        )

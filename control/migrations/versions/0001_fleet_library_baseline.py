"""Create the fresh Fleet and Library control-plane baseline."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_fleet_library_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_enrollment_grants',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=True),
    sa.Column('purpose', sa.String(length=24), server_default='new-node', nullable=False),
    sa.Column('token_digest', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("purpose = 'new-node'", name='ck_agent_enrollment_grants_purpose'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_digest')
    )
    op.create_index(op.f('ix_agent_enrollment_grants_created_at'), 'agent_enrollment_grants', ['created_at'], unique=False)
    op.create_index(op.f('ix_agent_enrollment_grants_expires_at'), 'agent_enrollment_grants', ['expires_at'], unique=False)
    op.create_index(op.f('ix_agent_enrollment_grants_node_id'), 'agent_enrollment_grants', ['node_id'], unique=False)
    op.create_table('agent_issued_certificate_revocations',
    sa.Column('serial', sa.String(length=128), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('provider_request_id', sa.String(length=64), nullable=False),
    sa.Column('fingerprint', sa.String(length=128), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ca_revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('serial'),
    sa.UniqueConstraint('provider_request_id')
    )
    op.create_index(op.f('ix_agent_issued_certificate_revocations_node_id'), 'agent_issued_certificate_revocations', ['node_id'], unique=False)
    op.create_index(op.f('ix_agent_issued_certificate_revocations_state'), 'agent_issued_certificate_revocations', ['state'], unique=False)
    op.create_table('agent_nodes',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('protocol_version', sa.Integer(), nullable=True),
    sa.Column('architecture', sa.String(length=16), nullable=True),
    sa.Column('semantic_version', sa.String(length=32), nullable=True),
    sa.Column('build_digest', sa.String(length=71), nullable=True),
    sa.Column('binary_digest', sa.String(length=64), nullable=True),
    sa.Column('self_test_passed', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('contact_certificate_serial', sa.String(length=128), nullable=True),
    sa.Column('contact_observation_digest', sa.String(length=64), nullable=True),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("architecture IS NULL OR architecture IN ('linux-amd64', 'linux-arm64')", name='ck_agent_nodes_architecture'),
    sa.PrimaryKeyConstraint('node_id')
    )
    op.create_table('audit_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=120), nullable=False),
    sa.Column('authority_revision', sa.String(length=128), nullable=True),
    sa.Column('targets', sa.JSON(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_events_occurred_at'), 'audit_events', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_audit_events_request_id'), 'audit_events', ['request_id'], unique=False)
    op.create_table('control_authority_revisions',
    sa.Column('revision_id', sa.String(length=64), nullable=False),
    sa.Column('parent_revision', sa.String(length=64), nullable=True),
    sa.Column('documents', sa.JSON(), nullable=False),
    sa.Column('dependencies', sa.JSON(), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_revision'], ['control_authority_revisions.revision_id']),
    sa.PrimaryKeyConstraint('revision_id')
    )
    op.create_index(op.f('ix_control_authority_revisions_parent_revision'), 'control_authority_revisions', ['parent_revision'], unique=False)
    op.create_index(op.f('ix_control_authority_revisions_created_at'), 'control_authority_revisions', ['created_at'], unique=False)
    op.create_table('control_authority_heads',
    sa.Column('singleton_id', sa.Integer(), nullable=False),
    sa.Column('revision_id', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['revision_id'], ['control_authority_revisions.revision_id']),
    sa.PrimaryKeyConstraint('singleton_id'),
    sa.UniqueConstraint('revision_id')
    )
    op.create_table('control_authority_proposals',
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('base_revision', sa.String(length=64), nullable=False),
    sa.Column('changes', sa.JSON(), nullable=False),
    sa.Column('patch', sa.LargeBinary(), nullable=False),
    sa.Column('affected_documents', sa.JSON(), nullable=False),
    sa.Column('validation_results', sa.JSON(), nullable=False),
    sa.Column('applied_revision', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['applied_revision'], ['control_authority_revisions.revision_id']),
    sa.ForeignKeyConstraint(['base_revision'], ['control_authority_revisions.revision_id']),
    sa.PrimaryKeyConstraint('digest')
    )
    op.create_index(op.f('ix_control_authority_proposals_base_revision'), 'control_authority_proposals', ['base_revision'], unique=False)
    op.create_index(op.f('ix_control_authority_proposals_applied_revision'), 'control_authority_proposals', ['applied_revision'], unique=False)
    op.create_index(op.f('ix_control_authority_proposals_created_at'), 'control_authority_proposals', ['created_at'], unique=False)
    op.create_table('catalog_entities',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('publisher', sa.String(length=63), nullable=False),
    sa.Column('slug', sa.String(length=63), nullable=False),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('model-group','model','model-version','execution-harness','runtime-distribution','patch-bundle')", name='ck_catalog_entities_kind'),
    sa.CheckConstraint('length(title) BETWEEN 1 AND 120', name='ck_catalog_entities_title'),
    sa.CheckConstraint('publisher = lower(publisher) AND length(publisher) BETWEEN 2 AND 63', name='ck_catalog_entities_publisher'),
    sa.CheckConstraint('slug = lower(slug) AND length(slug) BETWEEN 2 AND 63', name='ck_catalog_entities_slug'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kind', 'publisher', 'slug', name='uq_catalog_entities_identity')
    )
    op.create_index(op.f('ix_catalog_entities_kind'), 'catalog_entities', ['kind'], unique=False)
    op.create_index(op.f('ix_catalog_entities_publisher'), 'catalog_entities', ['publisher'], unique=False)
    op.create_index(op.f('ix_catalog_entities_slug'), 'catalog_entities', ['slug'], unique=False)
    op.create_index(op.f('ix_catalog_entities_updated_at'), 'catalog_entities', ['updated_at'], unique=False)
    op.create_table('control_process_heartbeats',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('process_kind', sa.String(length=16), nullable=False),
    sa.Column('process_instance_id', sa.String(length=64), nullable=False),
    sa.Column('loop_sequence', sa.BigInteger(), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("length(process_instance_id) = 64 AND process_instance_id = lower(process_instance_id) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(process_instance_id, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_control_process_heartbeats_process_instance_id'),
    sa.CheckConstraint("process_kind = 'worker'", name='ck_control_process_heartbeats_process_kind'),
    sa.CheckConstraint('(loop_sequence = 0 AND completed_at IS NULL) OR (loop_sequence >= 1 AND completed_at IS NOT NULL)', name='ck_control_process_heartbeats_loop_sequence'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('process_kind', name='uq_control_process_heartbeats_kind')
    )
    op.create_index(op.f('ix_control_process_heartbeats_completed_at'), 'control_process_heartbeats', ['completed_at'], unique=False)
    op.create_table('fleet_event_cursor',
    sa.Column('singleton_id', sa.SmallInteger(), nullable=False),
    sa.Column('last_id', sa.BigInteger(), nullable=False),
    sa.CheckConstraint('last_id >= 0', name='ck_fleet_event_cursor_last_id'),
    sa.CheckConstraint('singleton_id = 1', name='ck_fleet_event_cursor_singleton'),
    sa.PrimaryKeyConstraint('singleton_id')
    )
    op.execute(
        sa.text(
            "INSERT INTO fleet_event_cursor (singleton_id, last_id) VALUES (1, 0)"
        )
    )
    op.create_table('fleet_stream_events',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=True),
    sa.Column('entity_kind', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.String(length=128), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("event_type IN ('node-telemetry','recipe-state','operation-state')", name='ck_fleet_stream_events_event_type'),
    sa.CheckConstraint('expires_at > occurred_at', name='ck_fleet_stream_events_expiry'),
    sa.CheckConstraint('octet_length(CAST(payload AS TEXT)) BETWEEN 2 AND 8192', name='ck_fleet_stream_events_payload_size'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fleet_stream_events_expires_id', 'fleet_stream_events', ['expires_at', 'id'], unique=False)
    op.create_index('ix_fleet_stream_events_node_id', 'fleet_stream_events', ['node_id', 'id'], unique=False)
    op.create_table('local_recipes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('source_kind', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("source_kind IN ('local','workload_run','global','recipe_library')", name='ck_local_recipes_source_kind'),
    sa.CheckConstraint('slug = lower(slug) AND length(slug) BETWEEN 2 AND 128', name='ck_local_recipes_slug'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_index(op.f('ix_local_recipes_source_kind'), 'local_recipes', ['source_kind'], unique=False)
    op.create_table('observations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_observations_kind_node_observed', 'observations', ['kind', 'node_id', 'observed_at'], unique=False)
    op.create_index(op.f('ix_observations_node_id'), 'observations', ['node_id'], unique=False)
    op.create_index(op.f('ix_observations_observed_at'), 'observations', ['observed_at'], unique=False)
    op.create_table('recipe_source_bundles',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('media_type', sa.String(length=96), nullable=False),
    sa.Column('archive_bytes', sa.BigInteger(), nullable=False),
    sa.Column('total_bytes', sa.BigInteger(), nullable=False),
    sa.Column('file_count', sa.Integer(), nullable=False),
    sa.Column('storage_key', sa.String(length=255), nullable=False),
    sa.Column('manifest', sa.JSON(), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(sha256) = 64 AND sha256 = lower(sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_source_bundle_digest'),
    sa.CheckConstraint('archive_bytes > 0 AND total_bytes >= 0 AND file_count >= 1', name='ck_recipe_source_bundle_sizes'),
    sa.PrimaryKeyConstraint('sha256'),
    sa.UniqueConstraint('storage_key')
    )
    op.create_table('source_bundle_archives',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('archive', sa.LargeBinary(), nullable=False),
    sa.ForeignKeyConstraint(['sha256'], ['recipe_source_bundles.sha256'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('sha256')
    )
    op.create_table('reconciliation_completion_generation',
    sa.Column('singleton_id', sa.Integer(), nullable=False),
    sa.Column('last_generation', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('singleton_id')
    )
    op.create_table('reconciliations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('authority_revision', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('graph', sa.JSON(), server_default='{"authority_revision":"","nodes":[],"schema_version":1,"targets":[]}', nullable=False),
    sa.Column('graph_digest', sa.String(length=64), server_default='5c061eb8dfce0a3f2bcbfbf06cb71d695c33e8f4269e17bfe5cd1cda0054cdc5', nullable=False),
    sa.Column('plan_digest', sa.String(length=64), nullable=True),
    sa.Column('resolved_plan', sa.JSON(), nullable=True),
    sa.Column('current_phase', sa.String(length=32), server_default='planned', nullable=False),
    sa.Column('route_withdrawal_generation', sa.Integer(), server_default='0', nullable=False),
    sa.Column('terminal_reason', sa.Text(), nullable=True),
    sa.Column('completion_generation', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_reconciliations_completion_generation'), 'reconciliations', ['completion_generation'], unique=True)
    op.create_index(op.f('ix_reconciliations_created_at'), 'reconciliations', ['created_at'], unique=False)
    op.create_index(op.f('ix_reconciliations_plan_digest'), 'reconciliations', ['plan_digest'], unique=True)
    op.create_table('telemetry_maintenance_state',
    sa.Column('singleton_id', sa.SmallInteger(), nullable=False),
    sa.Column('next_resolution_seconds', sa.SmallInteger(), nullable=False),
    sa.CheckConstraint('next_resolution_seconds IN (60, 900)', name='ck_telemetry_maintenance_state_resolution'),
    sa.CheckConstraint('singleton_id = 1', name='ck_telemetry_maintenance_state_singleton'),
    sa.PrimaryKeyConstraint('singleton_id')
    )
    op.execute(
        sa.text(
            "INSERT INTO telemetry_maintenance_state "
            "(singleton_id, next_resolution_seconds) VALUES (1, 60)"
        )
    )
    op.create_table('users',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('password_verifier', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('subject')
    )
    op.create_table('agent_certificate_rotations',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('source_serial', sa.String(length=128), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('csr_pem', sa.Text(), nullable=False),
    sa.Column('csr_public_key_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('provider_request_id', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id'),
    sa.UniqueConstraint('provider_request_id')
    )
    op.create_index(op.f('ix_agent_certificate_rotations_state'), 'agent_certificate_rotations', ['state'], unique=False)
    op.create_table('agent_certificates',
    sa.Column('serial', sa.String(length=128), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('not_before', sa.DateTime(timezone=True), nullable=False),
    sa.Column('not_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('fingerprint', sa.String(length=128), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('certificate_pem', sa.Text(), nullable=True),
    sa.Column('chain_pem', sa.Text(), nullable=True),
    sa.Column('csr_public_key_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ca_revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ),
    sa.PrimaryKeyConstraint('serial'),
    sa.UniqueConstraint('fingerprint'),
    sa.UniqueConstraint('node_id', 'generation')
    )
    op.create_index(op.f('ix_agent_certificates_node_id'), 'agent_certificates', ['node_id'], unique=False)
    op.create_table('agent_enrollments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('grant_id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('csr_pem', sa.Text(), nullable=False),
    sa.Column('csr_public_key_pem', sa.Text(), nullable=False),
    sa.Column('csr_public_key_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('host_key_fingerprint', sa.String(length=512), nullable=False),
    sa.Column('hardware_fingerprint', sa.String(length=512), nullable=False),
    sa.Column('agent_digest', sa.String(length=128), nullable=False),
    sa.Column('boot_id', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decision_actor', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('certificate_pem', sa.Text(), nullable=True),
    sa.Column('chain_pem', sa.Text(), nullable=True),
    sa.Column('certificate_serial', sa.String(length=128), nullable=True),
    sa.Column('certificate_fingerprint', sa.String(length=128), nullable=True),
    sa.Column('certificate_generation', sa.Integer(), nullable=True),
    sa.Column('certificate_not_before', sa.DateTime(timezone=True), nullable=True),
    sa.Column('certificate_not_after', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['grant_id'], ['agent_enrollment_grants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('certificate_fingerprint'),
    sa.UniqueConstraint('certificate_serial'),
    sa.UniqueConstraint('grant_id')
    )
    op.create_index(op.f('ix_agent_enrollments_created_at'), 'agent_enrollments', ['created_at'], unique=False)
    op.create_index(op.f('ix_agent_enrollments_node_id'), 'agent_enrollments', ['node_id'], unique=False)
    op.create_index(op.f('ix_agent_enrollments_state'), 'agent_enrollments', ['state'], unique=False)
    op.create_table('agent_node_profiles',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('hostname', sa.String(length=255), nullable=False),
    sa.Column('lifecycle', sa.String(length=64), server_default='managed', nullable=False),
    sa.Column('labels', sa.JSON(), nullable=False),
    sa.CheckConstraint('length(display_name) BETWEEN 1 AND 200', name='ck_agent_node_profiles_display_name_length'),
    sa.CheckConstraint('length(hostname) <= 255', name='ck_agent_node_profiles_hostname_length'),
    sa.CheckConstraint('length(lifecycle) BETWEEN 1 AND 64', name='ck_agent_node_profiles_lifecycle_length'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id')
    )
    op.create_table('agent_profiles',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('hostname', sa.String(length=255), nullable=False),
    sa.Column('lifecycle', sa.String(length=16), nullable=False),
    sa.Column('labels', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id')
    )

    op.create_table('recipes',
    sa.Column('recipe_id', sa.String(length=128), nullable=False),
    sa.Column('slug', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('recipe_id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('recipe_revisions',
    sa.Column('revision_id', sa.String(length=128), nullable=False),
    sa.Column('recipe_id', sa.String(length=128), nullable=False),
    sa.Column('revision_number', sa.Integer(), nullable=False),
    sa.Column('content', sa.JSON(), nullable=False),
    sa.Column('content_digest', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('revision_number >= 1', name='ck_recipe_revision_number'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.recipe_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('revision_id'),
    sa.UniqueConstraint('recipe_id', 'revision_number', name='uq_recipe_revision_number'),
    sa.UniqueConstraint('recipe_id', 'content_digest', name='uq_recipe_revision_digest')
    )
    op.create_index(op.f('ix_recipe_revisions_recipe_id'), 'recipe_revisions', ['recipe_id'], unique=False)
    op.create_index(op.f('ix_recipe_revisions_content_digest'), 'recipe_revisions', ['content_digest'], unique=False)

    op.create_table('catalog_entity_revisions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('entity_id', sa.String(length=36), nullable=False),
    sa.Column('revision_number', sa.Integer(), nullable=False),
    sa.Column('lifecycle', sa.String(length=16), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('document', sa.JSON(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("content_sha256 IS NULL OR (length(content_sha256) = 64 AND content_sha256 = lower(content_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)", name='ck_catalog_entity_revisions_content_digest'),
    sa.CheckConstraint("lifecycle != 'resolved' OR content_sha256 IS NOT NULL", name='ck_catalog_entity_revisions_resolved_digest'),
    sa.CheckConstraint("lifecycle IN ('draft','blocked','resolved','deprecated')", name='ck_catalog_entity_revisions_lifecycle'),
    sa.CheckConstraint('revision_number >= 1', name='ck_catalog_entity_revisions_number'),
    sa.CheckConstraint('schema_version = 1', name='ck_catalog_entity_revisions_schema'),
    sa.ForeignKeyConstraint(['entity_id'], ['catalog_entities.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id', 'revision_number', name='uq_catalog_entity_revision_number')
    )
    op.create_index(op.f('ix_catalog_entity_revisions_content_sha256'), 'catalog_entity_revisions', ['content_sha256'], unique=False)
    op.create_index(op.f('ix_catalog_entity_revisions_entity_id'), 'catalog_entity_revisions', ['entity_id'], unique=False)
    op.create_index(op.f('ix_catalog_entity_revisions_lifecycle'), 'catalog_entity_revisions', ['lifecycle'], unique=False)
    op.create_table('jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('authority_revision', sa.String(length=128), nullable=False),
    sa.Column('targets', sa.JSON(), nullable=False),
    sa.Column('payload_digest', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('status_reason', sa.Text(), nullable=True),
    sa.Column('current_attempt', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('reconciliation_id', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['reconciliation_id'], ['reconciliations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_index(op.f('ix_jobs_created_at'), 'jobs', ['created_at'], unique=False)
    op.create_index(op.f('ix_jobs_reconciliation_id'), 'jobs', ['reconciliation_id'], unique=True)
    op.create_index(op.f('ix_jobs_state'), 'jobs', ['state'], unique=False)
    op.create_table('job_log_entries',
    sa.Column('job_id', sa.String(length=36), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('job_id', 'digest')
    )
    op.create_index(op.f('ix_job_log_entries_created_at'), 'job_log_entries', ['created_at'], unique=False)
    op.create_table('local_recipe_revisions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('revision_number', sa.Integer(), nullable=False),
    sa.Column('lifecycle', sa.String(length=16), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('document', sa.JSON(), nullable=False),
    sa.Column('content_sha256', sa.String(length=64), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("content_sha256 IS NULL OR (length(content_sha256) = 64 AND content_sha256 = lower(content_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)", name='ck_local_recipe_revisions_content_digest'),
    sa.CheckConstraint("lifecycle != 'resolved' OR content_sha256 IS NOT NULL", name='ck_local_recipe_revisions_resolved_digest'),
    sa.CheckConstraint("lifecycle IN ('draft','blocked','resolved','deprecated')", name='ck_local_recipe_revisions_lifecycle'),
    sa.CheckConstraint('revision_number >= 1', name='ck_local_recipe_revisions_number'),
    sa.CheckConstraint('schema_version >= 1', name='ck_local_recipe_revisions_schema'),
    sa.ForeignKeyConstraint(['recipe_id'], ['local_recipes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'content_sha256', name='uq_local_recipe_revision_content'),
    sa.UniqueConstraint('recipe_id', 'revision_number', name='uq_local_recipe_revision_number')
    )
    op.create_index(op.f('ix_local_recipe_revisions_content_sha256'), 'local_recipe_revisions', ['content_sha256'], unique=False)
    op.create_index(op.f('ix_local_recipe_revisions_lifecycle'), 'local_recipe_revisions', ['lifecycle'], unique=False)
    op.create_index(op.f('ix_local_recipe_revisions_recipe_id'), 'local_recipe_revisions', ['recipe_id'], unique=False)
    op.create_table('node_artifacts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('ref_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('image','image-layer','model','auxiliary')", name='ck_node_artifacts_kind'),
    sa.CheckConstraint("length(digest) = 64 AND digest = lower(digest) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_node_artifacts_digest'),
    sa.CheckConstraint("state IN ('partial','verified','missing','corrupt')", name='ck_node_artifacts_state'),
    sa.CheckConstraint('size_bytes>=0 AND ref_count>=0', name='ck_node_artifacts_sizes'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('node_id', 'digest', name='uq_node_artifact_digest')
    )
    op.create_index(op.f('ix_node_artifacts_node_id'), 'node_artifacts', ['node_id'], unique=False)
    op.create_table('node_inventory_snapshots',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('disk_total_bytes', sa.BigInteger(), nullable=False),
    sa.Column('disk_free_bytes', sa.BigInteger(), nullable=False),
    sa.Column('host_memory_total_bytes', sa.BigInteger(), nullable=False),
    sa.Column('host_memory_free_bytes', sa.BigInteger(), nullable=False),
    sa.Column('gpu_memory_total_bytes', sa.BigInteger(), nullable=False),
    sa.Column('gpu_memory_free_bytes', sa.BigInteger(), nullable=False),
    sa.Column('gpu_count', sa.Integer(), nullable=False),
    sa.Column('fabric_address', sa.String(length=45), nullable=True),
    sa.Column('fabric_bandwidth_mbps', sa.BigInteger(), nullable=True),
    sa.Column('nvidia_driver_version', sa.String(length=256), server_default='unknown', nullable=False),
    sa.Column('container_runtime_version', sa.String(length=256), server_default='unknown', nullable=False),
    sa.Column('artifact_store_read_only', sa.Boolean(), nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('evidence_digest', sa.String(length=64), nullable=False),
    sa.CheckConstraint("length(evidence_digest) = 64 AND evidence_digest = lower(evidence_digest) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(evidence_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_inventory_digest'),
    sa.CheckConstraint('(fabric_address IS NULL AND fabric_bandwidth_mbps IS NULL) OR (fabric_address IS NOT NULL AND fabric_bandwidth_mbps>0)', name='ck_inventory_fabric'),
    sa.CheckConstraint('disk_total_bytes>=0 AND disk_free_bytes>=0 AND disk_free_bytes<=disk_total_bytes', name='ck_inventory_disk'),
    sa.CheckConstraint('gpu_memory_total_bytes>=0 AND gpu_memory_free_bytes>=0 AND gpu_memory_free_bytes<=gpu_memory_total_bytes AND gpu_count>=0', name='ck_inventory_gpu_memory'),
    sa.CheckConstraint('host_memory_total_bytes>=0 AND host_memory_free_bytes>=0 AND host_memory_free_bytes<=host_memory_total_bytes', name='ck_inventory_host_memory'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('evidence_digest'),
    sa.UniqueConstraint('node_id', 'observed_at', name='uq_inventory_node_observed')
    )
    op.create_index('ix_inventory_node_observed', 'node_inventory_snapshots', ['node_id', 'observed_at'], unique=False)
    op.create_table('node_mutation_leases',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('owner_kind', sa.String(length=32), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('fence', sa.String(length=36), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(fence) = 36 AND substr(fence, 9, 1) = '-' AND substr(fence, 14, 1) = '-' AND substr(fence, 19, 1) = '-' AND substr(fence, 24, 1) = '-' AND (length(replace(fence, '-', '')) = 32 AND replace(fence, '-', '') = lower(replace(fence, '-', '')) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(fence, '-', ''), '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)", name='ck_node_mutation_leases_fence_shape'),
    sa.CheckConstraint("length(owner_id) = 36 AND substr(owner_id, 9, 1) = '-' AND substr(owner_id, 14, 1) = '-' AND substr(owner_id, 19, 1) = '-' AND substr(owner_id, 24, 1) = '-' AND (length(replace(owner_id, '-', '')) = 32 AND replace(owner_id, '-', '') = lower(replace(owner_id, '-', '')) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(owner_id, '-', ''), '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)", name='ck_node_mutation_leases_owner_id_shape'),
    sa.CheckConstraint("owner_kind = 'reconciliation'", name='ck_node_mutation_leases_owner_kind'),
    sa.CheckConstraint("state IN ('held', 'releasing')", name='ck_node_mutation_leases_state'),
    sa.CheckConstraint('updated_at >= acquired_at', name='ck_node_mutation_leases_timestamp_order'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id')
    )
    op.create_index('ix_node_mutation_leases_owner', 'node_mutation_leases', ['owner_kind', 'owner_id'], unique=False)
    op.create_table('node_telemetry_rollup_buckets',
    sa.Column('resolution_seconds', sa.SmallInteger(), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('source_sample_count', sa.BigInteger(), nullable=False),
    sa.Column('gap_samples', sa.BigInteger(), nullable=False),
    sa.CheckConstraint('resolution_seconds IN (60, 900)', name='ck_telemetry_rollup_buckets_resolution'),
    sa.CheckConstraint('source_sample_count BETWEEN 0 AND 9223372036854775807 AND gap_samples BETWEEN 0 AND 9223372036854775807', name='ck_telemetry_rollup_buckets_counts'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], name='fk_telemetry_rollup_buckets_node', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('resolution_seconds', 'node_id', 'bucket_start')
    )
    op.create_index('ix_telemetry_rollup_buckets_resolution_start', 'node_telemetry_rollup_buckets', ['resolution_seconds', 'bucket_start', 'node_id'], unique=False)
    op.create_table('node_telemetry_rollup_dirty',
    sa.Column('resolution_seconds', sa.SmallInteger(), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('resolution_seconds IN (60, 900)', name='ck_telemetry_rollup_dirty_resolution'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], name='fk_telemetry_rollup_dirty_node', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('resolution_seconds', 'node_id', 'bucket_start')
    )
    op.create_index('ix_telemetry_rollup_dirty_resolution_start', 'node_telemetry_rollup_dirty', ['resolution_seconds', 'bucket_start', 'node_id'], unique=False)
    op.create_table('node_telemetry_samples',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('boot_id', sa.String(length=36), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cpu_utilization_percent', sa.Float(), nullable=True),
    sa.Column('load_average_1m', sa.Float(), nullable=True),
    sa.Column('memory_total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('memory_available_bytes', sa.BigInteger(), nullable=True),
    sa.Column('disk_total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('disk_free_bytes', sa.BigInteger(), nullable=True),
    sa.Column('gpu_utilization_percent', sa.Float(), nullable=True),
    sa.Column('gpu_memory_total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('gpu_memory_free_bytes', sa.BigInteger(), nullable=True),
    sa.Column('temperature_c', sa.Float(), nullable=True),
    sa.Column('power_watts', sa.Float(), nullable=True),
    sa.Column('network_receive_bytes_per_second', sa.Float(), nullable=True),
    sa.Column('network_transmit_bytes_per_second', sa.Float(), nullable=True),
    sa.Column('gap_samples', sa.BigInteger(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.CheckConstraint("length(boot_id) = 36 AND substr(boot_id, 9, 1) = '-' AND substr(boot_id, 14, 1) = '-' AND substr(boot_id, 19, 1) = '-' AND substr(boot_id, 24, 1) = '-' AND (length(replace(boot_id, '-', '')) = 32 AND replace(boot_id, '-', '') = lower(replace(boot_id, '-', '')) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(boot_id, '-', ''), '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0)", name='ck_telemetry_boot_id_shape'),
    sa.CheckConstraint('(cpu_utilization_percent IS NULL OR cpu_utilization_percent BETWEEN 0 AND 100) AND (gpu_utilization_percent IS NULL OR gpu_utilization_percent BETWEEN 0 AND 100)', name='ck_telemetry_utilization'),
    sa.CheckConstraint('(disk_total_bytes IS NULL AND disk_free_bytes IS NULL) OR (disk_total_bytes IS NOT NULL AND disk_free_bytes IS NOT NULL AND disk_total_bytes >= 0 AND disk_free_bytes >= 0 AND disk_total_bytes <= 17592186044416 AND disk_free_bytes <= 17592186044416 AND disk_free_bytes <= disk_total_bytes)', name='ck_telemetry_disk'),
    sa.CheckConstraint('(gpu_memory_total_bytes IS NULL AND gpu_memory_free_bytes IS NULL) OR (gpu_memory_total_bytes IS NOT NULL AND gpu_memory_free_bytes IS NOT NULL AND gpu_memory_total_bytes >= 0 AND gpu_memory_free_bytes >= 0 AND gpu_memory_total_bytes <= 17592186044416 AND gpu_memory_free_bytes <= 17592186044416 AND gpu_memory_free_bytes <= gpu_memory_total_bytes)', name='ck_telemetry_gpu_memory'),
    sa.CheckConstraint('(memory_total_bytes IS NULL AND memory_available_bytes IS NULL) OR (memory_total_bytes IS NOT NULL AND memory_available_bytes IS NOT NULL AND memory_total_bytes >= 0 AND memory_available_bytes >= 0 AND memory_total_bytes <= 17592186044416 AND memory_available_bytes <= 17592186044416 AND memory_available_bytes <= memory_total_bytes)', name='ck_telemetry_memory'),
    sa.CheckConstraint('(temperature_c IS NULL OR temperature_c BETWEEN -100 AND 300) AND (power_watts IS NULL OR power_watts BETWEEN 0 AND 100000) AND (network_receive_bytes_per_second IS NULL OR network_receive_bytes_per_second BETWEEN 0 AND 1000000000000000) AND (network_transmit_bytes_per_second IS NULL OR network_transmit_bytes_per_second BETWEEN 0 AND 1000000000000000)', name='ck_telemetry_physical_metrics'),
    sa.CheckConstraint('length(CAST(details AS TEXT)) BETWEEN 2 AND 4096', name='ck_telemetry_details'),
    sa.CheckConstraint('load_average_1m IS NULL OR load_average_1m BETWEEN 0 AND 1000000', name='ck_telemetry_load'),
    sa.CheckConstraint('sequence BETWEEN 0 AND 9223372036854775807 AND gap_samples BETWEEN 0 AND 9223372036854775807', name='ck_telemetry_sequences'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('node_id', 'boot_id', 'sequence', name='uq_telemetry_node_boot_sequence'),
    sa.UniqueConstraint('node_id', 'id', name='uq_telemetry_node_sample')
    )
    op.create_index('ix_telemetry_node_observed', 'node_telemetry_samples', ['node_id', 'observed_at'], unique=False)
    op.create_table('recipe_global_links',
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('global_recipe_id', sa.String(length=36), nullable=False),
    sa.Column('global_publisher', sa.String(length=63), nullable=False),
    sa.Column('global_slug', sa.String(length=63), nullable=False),
    sa.Column('global_revision', sa.Integer(), nullable=False),
    sa.Column('global_content_sha256', sa.String(length=64), nullable=False),
    sa.Column('sync_state', sa.String(length=24), nullable=False),
    sa.Column('synced_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(global_content_sha256) = 64 AND global_content_sha256 = lower(global_content_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(global_content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_global_links_digest'),
    sa.CheckConstraint("sync_state IN ('current','local-ahead','remote-ahead','unavailable')", name='ck_recipe_global_links_state'),
    sa.CheckConstraint('global_revision >= 1', name='ck_recipe_global_links_revision'),
    sa.ForeignKeyConstraint(['recipe_id'], ['local_recipes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('recipe_id'),
    sa.UniqueConstraint('global_publisher', 'global_slug', name='uq_recipe_global_link_identity')
    )
    op.create_index(op.f('ix_recipe_global_links_global_recipe_id'), 'recipe_global_links', ['global_recipe_id'], unique=False)
    op.create_table('recipe_imports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('source_kind', sa.String(length=16), nullable=False),
    sa.Column('source_reference', sa.Text(), nullable=False),
    sa.Column('source_sha256', sa.String(length=64), nullable=False),
    sa.Column('redacted_source', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(source_sha256) = 64 AND source_sha256 = lower(source_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(source_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_imports_source_digest'),
    sa.CheckConstraint("source_kind IN ('local','workload_run','global','recipe_library')", name='ck_recipe_imports_source_kind'),
    sa.ForeignKeyConstraint(['recipe_id'], ['local_recipes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_kind', 'source_sha256', name='uq_recipe_import_source')
    )
    op.create_index(op.f('ix_recipe_imports_recipe_id'), 'recipe_imports', ['recipe_id'], unique=False)
    op.create_index(op.f('ix_recipe_imports_source_sha256'), 'recipe_imports', ['source_sha256'], unique=False)
    op.create_table('reconciliation_cancellations',
    sa.Column('reconciliation_id', sa.String(length=36), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("state IN ('requested', 'withdrawal-pending', 'withdrawn', 'processing', 'compensating', 'completed', 'waiting-for-operator')", name='ck_reconciliation_cancellations_state'),
    sa.CheckConstraint('length(reason) BETWEEN 1 AND 1024', name='ck_reconciliation_cancellations_reason_length'),
    sa.ForeignKeyConstraint(['reconciliation_id'], ['reconciliations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('reconciliation_id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_index(op.f('ix_reconciliation_cancellations_state'), 'reconciliation_cancellations', ['state'], unique=False)
    op.create_table('resource_reservations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('resource_key', sa.String(length=128), nullable=False),
    sa.Column('amount_bytes', sa.BigInteger(), nullable=False),
    sa.Column('owner_kind', sa.String(length=24), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('plan_digest', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('disk','unified-memory','host-memory','gpu-memory','port')", name='ck_reservations_kind'),
    sa.CheckConstraint("length(plan_digest) = 64 AND plan_digest = lower(plan_digest) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_reservations_digest'),
    sa.CheckConstraint("state IN ('active','released','expired') AND amount_bytes>=0", name='ck_reservations_state'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_reservations_node_state', 'resource_reservations', ['node_id', 'state'], unique=False)
    op.create_index('uq_active_node_port', 'resource_reservations', ['node_id', 'kind', 'resource_key'], unique=True, postgresql_where=sa.text("state='active' AND kind='port'"), sqlite_where=sa.text("state='active' AND kind='port'"))
    op.create_table('route_publication_owner',
    sa.Column('singleton_id', sa.Integer(), nullable=False),
    sa.Column('reconciliation_id', sa.String(length=36), nullable=True),
    sa.Column('owner_generation', sa.BigInteger(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('owner_generation >= 0', name='ck_route_publication_owner_generation'),
    sa.CheckConstraint('singleton_id = 1', name='ck_route_publication_owner_singleton'),
    sa.ForeignKeyConstraint(['reconciliation_id'], ['reconciliations.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('singleton_id'),
    sa.UniqueConstraint('reconciliation_id')
    )
    op.create_table('route_publications',
    sa.Column('reconciliation_id', sa.String(length=36), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('generation', sa.BigInteger(), nullable=True),
    sa.Column('plan_digest', sa.String(length=64), nullable=False),
    sa.Column('evidence_digest', sa.String(length=64), nullable=True),
    sa.Column('route_digest', sa.String(length=64), nullable=True),
    sa.Column('litellm_digest', sa.String(length=64), nullable=True),
    sa.Column('bundle_digest', sa.String(length=64), nullable=True),
    sa.Column('activation_marker', sa.JSON(), nullable=True),
    sa.Column('activation_marker_digest', sa.String(length=64), nullable=True),
    sa.Column('lease_issued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("state IN ('withdrawal-pending', 'routes-withdrawn', 'publication-pending', 'completed', 'failed')", name='ck_route_publications_state'),
    sa.CheckConstraint('activation_marker_digest IS NULL OR length(activation_marker_digest) = 64', name='ck_route_publications_activation_marker_digest_length'),
    sa.CheckConstraint('bundle_digest IS NULL OR length(bundle_digest) = 64', name='ck_route_publications_bundle_digest_length'),
    sa.CheckConstraint('evidence_digest IS NULL OR length(evidence_digest) = 64', name='ck_route_publications_evidence_digest_length'),
    sa.CheckConstraint('generation IS NULL OR generation >= 0', name='ck_route_publications_generation'),
    sa.CheckConstraint('lease_expires_at IS NULL OR (lease_issued_at IS NOT NULL AND lease_expires_at > lease_issued_at)', name='ck_route_publications_lease_window'),
    sa.CheckConstraint('length(plan_digest) = 64', name='ck_route_publications_plan_digest_length'),
    sa.CheckConstraint('litellm_digest IS NULL OR length(litellm_digest) = 64', name='ck_route_publications_litellm_digest_length'),
    sa.CheckConstraint('route_digest IS NULL OR length(route_digest) = 64', name='ck_route_publications_route_digest_length'),
    sa.ForeignKeyConstraint(['reconciliation_id'], ['reconciliations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('reconciliation_id')
    )
    op.create_index(op.f('ix_route_publications_generation'), 'route_publications', ['generation'], unique=True)
    op.create_index(op.f('ix_route_publications_state'), 'route_publications', ['state'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('digest')
    )
    op.create_index(op.f('ix_sessions_expires_at'), 'sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('agent_operations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('parent_job_id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=80), nullable=False),
    sa.Column('payload_digest', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('authority_revision', sa.String(length=128), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('current_attempt', sa.Integer(), nullable=False),
    sa.Column('retry_disposition', sa.String(length=32), nullable=True),
    sa.Column('retry_disposition_attempt', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ),
    sa.ForeignKeyConstraint(['parent_job_id'], ['jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_operations_created_at'), 'agent_operations', ['created_at'], unique=False)
    op.create_index(op.f('ix_agent_operations_node_id'), 'agent_operations', ['node_id'], unique=False)
    op.create_index(op.f('ix_agent_operations_parent_job_id'), 'agent_operations', ['parent_job_id'], unique=False)
    op.create_index(op.f('ix_agent_operations_state'), 'agent_operations', ['state'], unique=False)
    op.create_table('agent_presence',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('certificate_serial', sa.String(length=128), nullable=False),
    sa.Column('certificate_fingerprint', sa.String(length=128), nullable=False),
    sa.Column('management_address', sa.String(length=45), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(management_address) BETWEEN 2 AND 45', name='ck_agent_presence_management_address_length'),
    sa.ForeignKeyConstraint(['certificate_serial'], ['agent_certificates.serial'], ),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id')
    )
    op.create_index(op.f('ix_agent_presence_certificate_serial'), 'agent_presence', ['certificate_serial'], unique=False)
    op.create_index(op.f('ix_agent_presence_observed_at'), 'agent_presence', ['observed_at'], unique=False)
    op.create_table('cluster_mappings',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_revision_id', sa.String(length=36), nullable=False),
    sa.Column('topology_name', sa.String(length=64), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('node_count', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('parameters', sa.JSON(), nullable=False),
    sa.Column('placement_digest', sa.String(length=64), nullable=False),
    sa.Column('endpoint_owner_node_id', sa.String(length=36), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(placement_digest) = 64 AND placement_digest = lower(placement_digest) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(placement_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_cluster_mappings_placement_digest'),
    sa.CheckConstraint("state IN ('planned','ready','stale')", name='ck_cluster_mappings_state'),
    sa.CheckConstraint('generation >= 1', name='ck_cluster_mappings_generation'),
    sa.CheckConstraint('node_count >= 1', name='ck_cluster_mappings_node_count'),
    sa.ForeignKeyConstraint(['endpoint_owner_node_id'], ['agent_nodes.node_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recipe_revision_id'], ['local_recipe_revisions.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('placement_digest')
    )
    op.create_index(op.f('ix_cluster_mappings_recipe_revision_id'), 'cluster_mappings', ['recipe_revision_id'], unique=False)
    op.create_index(op.f('ix_cluster_mappings_state'), 'cluster_mappings', ['state'], unique=False)
    op.create_table('job_attempts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('fence', sa.String(length=36), nullable=False),
    sa.Column('worker_id', sa.String(length=200), nullable=False),
    sa.Column('lease_deadline', sa.DateTime(timezone=True), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('fence'),
    sa.UniqueConstraint('job_id', 'attempt')
    )
    op.create_index(op.f('ix_job_attempts_job_id'), 'job_attempts', ['job_id'], unique=False)
    op.create_index(op.f('ix_job_attempts_lease_deadline'), 'job_attempts', ['lease_deadline'], unique=False)
    op.create_table('node_telemetry_latest',
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('sample_id', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['node_id', 'sample_id'], ['node_telemetry_samples.node_id', 'node_telemetry_samples.id'], name='fk_telemetry_latest_node_sample', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('node_id'),
    sa.UniqueConstraint('sample_id')
    )
    op.create_table('node_telemetry_rollup_metrics',
    sa.Column('resolution_seconds', sa.SmallInteger(), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('bucket_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('metric_name', sa.String(length=64), nullable=False),
    sa.Column('sample_count', sa.BigInteger(), nullable=False),
    sa.Column('minimum', sa.Float(), nullable=False),
    sa.Column('mean', sa.Float(), nullable=False),
    sa.Column('maximum', sa.Float(), nullable=False),
    sa.CheckConstraint('length(metric_name) BETWEEN 1 AND 64', name='ck_telemetry_rollup_metrics_name'),
    sa.CheckConstraint('minimum BETWEEN -1e308 AND 1e308 AND mean BETWEEN -1e308 AND 1e308 AND maximum BETWEEN -1e308 AND 1e308 AND minimum <= mean AND mean <= maximum', name='ck_telemetry_rollup_metrics_values'),
    sa.CheckConstraint('resolution_seconds IN (60, 900)', name='ck_telemetry_rollup_metrics_resolution'),
    sa.CheckConstraint('sample_count BETWEEN 0 AND 9223372036854775807', name='ck_telemetry_rollup_metrics_count'),
    sa.ForeignKeyConstraint(['resolution_seconds', 'node_id', 'bucket_start'], ['node_telemetry_rollup_buckets.resolution_seconds', 'node_telemetry_rollup_buckets.node_id', 'node_telemetry_rollup_buckets.bucket_start'], name='fk_telemetry_rollup_metrics_bucket', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('resolution_seconds', 'node_id', 'bucket_start', 'metric_name')
    )
    op.create_table('recipe_builds',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_revision_id', sa.String(length=36), nullable=False),
    sa.Column('builder_node_id', sa.String(length=36), nullable=False),
    sa.Column('source_bundle_sha256', sa.String(length=64), nullable=False),
    sa.Column('build_input_sha256', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('policy_report', sa.JSON(), nullable=False),
    sa.Column('plan', sa.JSON(), nullable=False),
    sa.Column('image_digest', sa.String(length=71), nullable=True),
    sa.Column('oci_layout_sha256', sa.String(length=64), nullable=True),
    sa.Column('image_bytes', sa.BigInteger(), nullable=True),
    sa.Column('error', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("image_digest IS NULL OR (length(image_digest) = 71 AND substr(image_digest, 1, 7) = 'sha256:')", name='ck_recipe_builds_image_digest'),
    sa.CheckConstraint("length(build_input_sha256) = 64 AND build_input_sha256 = lower(build_input_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(build_input_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_builds_input_digest'),
    sa.CheckConstraint("length(source_bundle_sha256) = 64 AND source_bundle_sha256 = lower(source_bundle_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(source_bundle_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_builds_source_digest'),
    sa.CheckConstraint("state IN ('planned','building','succeeded','failed')", name='ck_recipe_builds_state'),
    sa.CheckConstraint('image_bytes IS NULL OR image_bytes > 0', name='ck_recipe_builds_image_size'),
    sa.CheckConstraint('oci_layout_sha256 IS NULL OR length(oci_layout_sha256) = 64', name='ck_recipe_builds_layout_digest'),
    sa.ForeignKeyConstraint(['builder_node_id'], ['agent_nodes.node_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recipe_revision_id'], ['local_recipe_revisions.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_revision_id', 'builder_node_id', 'build_input_sha256', name='uq_recipe_build_input_builder')
    )
    op.create_index(op.f('ix_recipe_builds_builder_node_id'), 'recipe_builds', ['builder_node_id'], unique=False)
    op.create_index(op.f('ix_recipe_builds_recipe_revision_id'), 'recipe_builds', ['recipe_revision_id'], unique=False)
    op.create_index(op.f('ix_recipe_builds_state'), 'recipe_builds', ['state'], unique=False)
    op.create_table('recipe_import_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('import_id', sa.String(length=36), nullable=False),
    sa.Column('source_path', sa.Text(), nullable=False),
    sa.Column('disposition', sa.String(length=32), nullable=False),
    sa.Column('destination_path', sa.Text(), nullable=True),
    sa.Column('reason_code', sa.String(length=128), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('blocking', sa.Boolean(), nullable=False),
    sa.CheckConstraint("disposition IN ('imported','incorporated','resolved','transformed','resolution_required','overlay_required','unsupported_blocking','dropped_redundant')", name='ck_recipe_import_items_disposition'),
    sa.ForeignKeyConstraint(['import_id'], ['recipe_imports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_recipe_import_items_disposition'), 'recipe_import_items', ['disposition'], unique=False)
    op.create_index(op.f('ix_recipe_import_items_import_id'), 'recipe_import_items', ['import_id'], unique=False)
    op.create_table('recipe_test_reports',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_revision_id', sa.String(length=36), nullable=False),
    sa.Column('report_sha256', sa.String(length=64), nullable=False),
    sa.Column('report', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(report_sha256) = 64 AND report_sha256 = lower(report_sha256) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(report_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_test_reports_digest'),
    sa.ForeignKeyConstraint(['recipe_revision_id'], ['local_recipe_revisions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_revision_id', 'report_sha256', name='uq_recipe_test_report_digest')
    )
    op.create_index(op.f('ix_recipe_test_reports_recipe_revision_id'), 'recipe_test_reports', ['recipe_revision_id'], unique=False)
    op.create_table('agent_operation_attempts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('operation_id', sa.String(length=36), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('fence', sa.String(length=36), nullable=False),
    sa.Column('lease_deadline', sa.DateTime(timezone=True), nullable=False),
    sa.Column('agent_certificate_serial', sa.String(length=128), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('progress', sa.JSON(), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['agent_certificate_serial'], ['agent_certificates.serial'], ),
    sa.ForeignKeyConstraint(['operation_id'], ['agent_operations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('fence'),
    sa.UniqueConstraint('operation_id', 'attempt')
    )
    op.create_index(op.f('ix_agent_operation_attempts_agent_certificate_serial'), 'agent_operation_attempts', ['agent_certificate_serial'], unique=False)
    op.create_index(op.f('ix_agent_operation_attempts_lease_deadline'), 'agent_operation_attempts', ['lease_deadline'], unique=False)
    op.create_index(op.f('ix_agent_operation_attempts_operation_id'), 'agent_operation_attempts', ['operation_id'], unique=False)
    op.create_table('cluster_mapping_nodes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('mapping_id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('endpoint_owner', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(role) BETWEEN 1 AND 64', name='ck_cluster_mapping_nodes_role'),
    sa.CheckConstraint('rank >= 0', name='ck_cluster_mapping_nodes_rank'),
    sa.ForeignKeyConstraint(['mapping_id'], ['cluster_mappings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mapping_id', 'node_id', name='uq_cluster_mapping_node'),
    sa.UniqueConstraint('mapping_id', 'rank', name='uq_cluster_mapping_rank')
    )
    op.create_index(op.f('ix_cluster_mapping_nodes_mapping_id'), 'cluster_mapping_nodes', ['mapping_id'], unique=False)
    op.create_index(op.f('ix_cluster_mapping_nodes_node_id'), 'cluster_mapping_nodes', ['node_id'], unique=False)
    op.create_table('recipe_installations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_revision_id', sa.String(length=36), nullable=False),
    sa.Column('mapping_id', sa.String(length=36), nullable=False),
    sa.Column('mapping_generation', sa.Integer(), nullable=False),
    sa.Column('recipe_build_id', sa.String(length=36), nullable=False),
    sa.Column('image_digest', sa.String(length=71), nullable=False),
    sa.Column('plan_digest', sa.String(length=64), nullable=False),
    sa.Column('plan', sa.JSON(), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("length(plan_digest) = 64 AND plan_digest = lower(plan_digest) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_installations_digest'),
    sa.CheckConstraint("state IN ('planned','installing','installed','partial','failed','uninstalled')", name='ck_recipe_installations_state'),
    sa.ForeignKeyConstraint(['mapping_id'], ['cluster_mappings.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recipe_build_id'], ['recipe_builds.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recipe_revision_id'], ['local_recipe_revisions.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_digest')
    )
    op.create_index(op.f('ix_recipe_installations_mapping_id'), 'recipe_installations', ['mapping_id'], unique=False)
    op.create_index(op.f('ix_recipe_installations_recipe_build_id'), 'recipe_installations', ['recipe_build_id'], unique=False)
    op.create_index(op.f('ix_recipe_installations_recipe_revision_id'), 'recipe_installations', ['recipe_revision_id'], unique=False)
    op.create_table('reconciliation_operations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('reconciliation_id', sa.String(length=36), nullable=False),
    sa.Column('graph_operation_id', sa.String(length=128), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('agent_operation_id', sa.String(length=36), nullable=True),
    sa.Column('expected_payload_digest', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('result_digest', sa.String(length=64), nullable=True),
    sa.Column('evidence_digest', sa.String(length=64), nullable=True),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('compensated_graph_operation_id', sa.String(length=128), nullable=True),
    sa.CheckConstraint("role IN ('primary', 'compensation')", name='ck_reconciliation_operations_role'),
    sa.CheckConstraint("state IN ('planned', 'queued', 'running', 'succeeded', 'accepted', 'failed', 'waiting-for-operator', 'compensating', 'compensated', 'uncertain')", name='ck_reconciliation_operations_state'),
    sa.CheckConstraint('compensated_graph_operation_id IS NULL OR length(compensated_graph_operation_id) BETWEEN 1 AND 128', name='ck_reconciliation_operations_compensated_id_length'),
    sa.CheckConstraint('evidence_digest IS NULL OR length(evidence_digest) = 64', name='ck_reconciliation_operations_evidence_digest_length'),
    sa.CheckConstraint('length(expected_payload_digest) = 64', name='ck_reconciliation_operations_expected_payload_digest_length'),
    sa.CheckConstraint('length(graph_operation_id) BETWEEN 1 AND 128', name='ck_reconciliation_operations_graph_operation_id_length'),
    sa.CheckConstraint('result_digest IS NULL OR length(result_digest) = 64', name='ck_reconciliation_operations_result_digest_length'),
    sa.ForeignKeyConstraint(['agent_operation_id'], ['agent_operations.id'], ),
    sa.ForeignKeyConstraint(['reconciliation_id'], ['reconciliations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reconciliation_id', 'graph_operation_id', 'role', name='uq_reconciliation_operation_graph_role')
    )
    op.create_index(op.f('ix_reconciliation_operations_agent_operation_id'), 'reconciliation_operations', ['agent_operation_id'], unique=True)
    op.create_index(op.f('ix_reconciliation_operations_reconciliation_id'), 'reconciliation_operations', ['reconciliation_id'], unique=False)
    op.create_index(op.f('ix_reconciliation_operations_state'), 'reconciliation_operations', ['state'], unique=False)
    op.create_table('installation_nodes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('installation_id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('required_bytes', sa.BigInteger(), nullable=False),
    sa.Column('installed_bytes', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('evidence_digest', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('required_bytes>=0 AND installed_bytes>=0', name='ck_installation_nodes_bytes'),
    sa.ForeignKeyConstraint(['installation_id'], ['recipe_installations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('installation_id', 'node_id', name='uq_installation_node')
    )
    op.create_index(op.f('ix_installation_nodes_installation_id'), 'installation_nodes', ['installation_id'], unique=False)
    op.create_index(op.f('ix_installation_nodes_node_id'), 'installation_nodes', ['node_id'], unique=False)
    op.create_table('recipe_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('installation_id', sa.String(length=36), nullable=False),
    sa.Column('mapping_id', sa.String(length=36), nullable=False),
    sa.Column('mapping_generation', sa.Integer(), nullable=False),
    sa.Column('alias', sa.String(length=128), nullable=False),
    sa.Column('plan_digest', sa.String(length=64), nullable=False),
    sa.Column('plan', sa.JSON(), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('route_state', sa.String(length=24), server_default='withdrawn', nullable=False),
    sa.Column('route_generation', sa.BigInteger(), nullable=True),
    sa.Column('route_digest', sa.String(length=64), nullable=True),
    sa.Column('route_error', sa.String(length=512), nullable=True),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("length(plan_digest) = 64 AND plan_digest = lower(plan_digest) AND length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0", name='ck_recipe_runs_digest'),
    sa.CheckConstraint("route_state IN ('withdrawn','pending','published','failed')", name='ck_recipe_runs_route_state'),
    sa.CheckConstraint("state IN ('planned','starting','running','stopping','stopped','failed','lost')", name='ck_recipe_runs_state'),
    sa.CheckConstraint('route_digest IS NULL OR length(route_digest)=64', name='ck_recipe_runs_route_digest'),
    sa.CheckConstraint('route_generation IS NULL OR route_generation>=1', name='ck_recipe_runs_route_generation'),
    sa.ForeignKeyConstraint(['installation_id'], ['recipe_installations.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['mapping_id'], ['cluster_mappings.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_digest')
    )
    op.create_index(op.f('ix_recipe_runs_installation_id'), 'recipe_runs', ['installation_id'], unique=False)
    op.create_index(op.f('ix_recipe_runs_mapping_id'), 'recipe_runs', ['mapping_id'], unique=False)
    op.create_table('run_nodes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('node_id', sa.String(length=36), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('port', sa.Integer(), nullable=False),
    sa.Column('reserved_memory_bytes', sa.BigInteger(), nullable=False),
    sa.Column('observed_memory_bytes', sa.BigInteger(), nullable=True),
    sa.Column('endpoint', sa.JSON(), nullable=True),
    sa.Column('evidence_digest', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('length(role) BETWEEN 1 AND 64', name='ck_run_nodes_role'),
    sa.CheckConstraint('rank>=0 AND port BETWEEN 1024 AND 65535 AND reserved_memory_bytes>=0 AND (observed_memory_bytes IS NULL OR observed_memory_bytes>=0)', name='ck_run_nodes_resources'),
    sa.ForeignKeyConstraint(['node_id'], ['agent_nodes.node_id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['run_id'], ['recipe_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'node_id', name='uq_run_node'),
    sa.UniqueConstraint('run_id', 'rank', name='uq_run_rank')
    )
    op.create_index(op.f('ix_run_nodes_node_id'), 'run_nodes', ['node_id'], unique=False)
    op.create_index(op.f('ix_run_nodes_run_id'), 'run_nodes', ['run_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    op.drop_index(op.f('ix_run_nodes_run_id'), table_name='run_nodes')
    op.drop_index(op.f('ix_run_nodes_node_id'), table_name='run_nodes')
    op.drop_table('run_nodes')
    op.drop_index(op.f('ix_recipe_runs_mapping_id'), table_name='recipe_runs')
    op.drop_index(op.f('ix_recipe_runs_installation_id'), table_name='recipe_runs')
    op.drop_table('recipe_runs')
    op.drop_index(op.f('ix_installation_nodes_node_id'), table_name='installation_nodes')
    op.drop_index(op.f('ix_installation_nodes_installation_id'), table_name='installation_nodes')
    op.drop_table('installation_nodes')
    op.drop_index(op.f('ix_reconciliation_operations_state'), table_name='reconciliation_operations')
    op.drop_index(op.f('ix_reconciliation_operations_reconciliation_id'), table_name='reconciliation_operations')
    op.drop_index(op.f('ix_reconciliation_operations_agent_operation_id'), table_name='reconciliation_operations')
    op.drop_table('reconciliation_operations')
    op.drop_index(op.f('ix_recipe_installations_recipe_revision_id'), table_name='recipe_installations')
    op.drop_index(op.f('ix_recipe_installations_recipe_build_id'), table_name='recipe_installations')
    op.drop_index(op.f('ix_recipe_installations_mapping_id'), table_name='recipe_installations')
    op.drop_table('recipe_installations')
    op.drop_index(op.f('ix_cluster_mapping_nodes_node_id'), table_name='cluster_mapping_nodes')
    op.drop_index(op.f('ix_cluster_mapping_nodes_mapping_id'), table_name='cluster_mapping_nodes')
    op.drop_table('cluster_mapping_nodes')
    op.drop_index(op.f('ix_agent_operation_attempts_operation_id'), table_name='agent_operation_attempts')
    op.drop_index(op.f('ix_agent_operation_attempts_lease_deadline'), table_name='agent_operation_attempts')
    op.drop_index(op.f('ix_agent_operation_attempts_agent_certificate_serial'), table_name='agent_operation_attempts')
    op.drop_table('agent_operation_attempts')
    op.drop_index(op.f('ix_recipe_test_reports_recipe_revision_id'), table_name='recipe_test_reports')
    op.drop_table('recipe_test_reports')
    op.drop_index(op.f('ix_recipe_import_items_import_id'), table_name='recipe_import_items')
    op.drop_index(op.f('ix_recipe_import_items_disposition'), table_name='recipe_import_items')
    op.drop_table('recipe_import_items')
    op.drop_index(op.f('ix_recipe_builds_state'), table_name='recipe_builds')
    op.drop_index(op.f('ix_recipe_builds_recipe_revision_id'), table_name='recipe_builds')
    op.drop_index(op.f('ix_recipe_builds_builder_node_id'), table_name='recipe_builds')
    op.drop_table('recipe_builds')
    op.drop_table('node_telemetry_rollup_metrics')
    op.drop_table('node_telemetry_latest')
    op.drop_index(op.f('ix_job_attempts_lease_deadline'), table_name='job_attempts')
    op.drop_index(op.f('ix_job_attempts_job_id'), table_name='job_attempts')
    op.drop_table('job_attempts')
    op.drop_index(op.f('ix_cluster_mappings_state'), table_name='cluster_mappings')
    op.drop_index(op.f('ix_cluster_mappings_recipe_revision_id'), table_name='cluster_mappings')
    op.drop_table('cluster_mappings')
    op.drop_index(op.f('ix_agent_presence_observed_at'), table_name='agent_presence')
    op.drop_index(op.f('ix_agent_presence_certificate_serial'), table_name='agent_presence')
    op.drop_table('agent_presence')
    op.drop_index(op.f('ix_agent_operations_state'), table_name='agent_operations')
    op.drop_index(op.f('ix_agent_operations_parent_job_id'), table_name='agent_operations')
    op.drop_index(op.f('ix_agent_operations_node_id'), table_name='agent_operations')
    op.drop_index(op.f('ix_agent_operations_created_at'), table_name='agent_operations')
    op.drop_table('agent_operations')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_expires_at'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index(op.f('ix_route_publications_state'), table_name='route_publications')
    op.drop_index(op.f('ix_route_publications_generation'), table_name='route_publications')
    op.drop_table('route_publications')
    op.drop_table('route_publication_owner')
    op.drop_index('uq_active_node_port', table_name='resource_reservations', postgresql_where=sa.text("state='active' AND kind='port'"), sqlite_where=sa.text("state='active' AND kind='port'"))
    op.drop_index('ix_reservations_node_state', table_name='resource_reservations')
    op.drop_table('resource_reservations')
    op.drop_index(op.f('ix_reconciliation_cancellations_state'), table_name='reconciliation_cancellations')
    op.drop_table('reconciliation_cancellations')
    op.drop_index(op.f('ix_recipe_imports_source_sha256'), table_name='recipe_imports')
    op.drop_index(op.f('ix_recipe_imports_recipe_id'), table_name='recipe_imports')
    op.drop_table('recipe_imports')
    op.drop_index(op.f('ix_recipe_global_links_global_recipe_id'), table_name='recipe_global_links')
    op.drop_table('recipe_global_links')
    op.drop_index('ix_telemetry_node_observed', table_name='node_telemetry_samples')
    op.drop_table('node_telemetry_samples')
    op.drop_index('ix_telemetry_rollup_dirty_resolution_start', table_name='node_telemetry_rollup_dirty')
    op.drop_table('node_telemetry_rollup_dirty')
    op.drop_index('ix_telemetry_rollup_buckets_resolution_start', table_name='node_telemetry_rollup_buckets')
    op.drop_table('node_telemetry_rollup_buckets')
    op.drop_index('ix_node_mutation_leases_owner', table_name='node_mutation_leases')
    op.drop_table('node_mutation_leases')
    op.drop_index('ix_inventory_node_observed', table_name='node_inventory_snapshots')
    op.drop_table('node_inventory_snapshots')
    op.drop_index(op.f('ix_node_artifacts_node_id'), table_name='node_artifacts')
    op.drop_table('node_artifacts')
    op.drop_index(op.f('ix_local_recipe_revisions_recipe_id'), table_name='local_recipe_revisions')
    op.drop_index(op.f('ix_local_recipe_revisions_lifecycle'), table_name='local_recipe_revisions')
    op.drop_index(op.f('ix_local_recipe_revisions_content_sha256'), table_name='local_recipe_revisions')
    op.drop_table('local_recipe_revisions')
    op.drop_index(op.f('ix_job_log_entries_created_at'), table_name='job_log_entries')
    op.drop_table('job_log_entries')
    op.drop_index(op.f('ix_jobs_state'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_reconciliation_id'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_created_at'), table_name='jobs')
    op.drop_table('jobs')
    op.drop_index(op.f('ix_catalog_entity_revisions_lifecycle'), table_name='catalog_entity_revisions')
    op.drop_index(op.f('ix_catalog_entity_revisions_entity_id'), table_name='catalog_entity_revisions')
    op.drop_index(op.f('ix_catalog_entity_revisions_content_sha256'), table_name='catalog_entity_revisions')
    op.drop_table('catalog_entity_revisions')
    op.drop_index(op.f('ix_recipe_revisions_content_digest'), table_name='recipe_revisions')
    op.drop_index(op.f('ix_recipe_revisions_recipe_id'), table_name='recipe_revisions')
    op.drop_table('recipe_revisions')
    op.drop_table('recipes')
    op.drop_table('agent_profiles')
    op.drop_table('agent_node_profiles')
    op.drop_index(op.f('ix_agent_enrollments_state'), table_name='agent_enrollments')
    op.drop_index(op.f('ix_agent_enrollments_node_id'), table_name='agent_enrollments')
    op.drop_index(op.f('ix_agent_enrollments_created_at'), table_name='agent_enrollments')
    op.drop_table('agent_enrollments')
    op.drop_index(op.f('ix_agent_certificates_node_id'), table_name='agent_certificates')
    op.drop_table('agent_certificates')
    op.drop_index(op.f('ix_agent_certificate_rotations_state'), table_name='agent_certificate_rotations')
    op.drop_table('agent_certificate_rotations')
    op.drop_table('users')
    op.drop_table('telemetry_maintenance_state')
    op.drop_index(op.f('ix_reconciliations_plan_digest'), table_name='reconciliations')
    op.drop_index(op.f('ix_reconciliations_created_at'), table_name='reconciliations')
    op.drop_index(op.f('ix_reconciliations_completion_generation'), table_name='reconciliations')
    op.drop_table('reconciliations')
    op.drop_table('reconciliation_completion_generation')
    op.drop_table('source_bundle_archives')
    op.drop_table('recipe_source_bundles')
    op.drop_index(op.f('ix_observations_observed_at'), table_name='observations')
    op.drop_index(op.f('ix_observations_node_id'), table_name='observations')
    op.drop_index('ix_observations_kind_node_observed', table_name='observations')
    op.drop_table('observations')
    op.drop_index(op.f('ix_local_recipes_source_kind'), table_name='local_recipes')
    op.drop_table('local_recipes')
    op.drop_index('ix_fleet_stream_events_node_id', table_name='fleet_stream_events')
    op.drop_index('ix_fleet_stream_events_expires_id', table_name='fleet_stream_events')
    op.drop_table('fleet_stream_events')
    op.drop_table('fleet_event_cursor')
    op.drop_index(op.f('ix_control_process_heartbeats_completed_at'), table_name='control_process_heartbeats')
    op.drop_table('control_process_heartbeats')
    op.drop_index(op.f('ix_catalog_entities_updated_at'), table_name='catalog_entities')
    op.drop_index(op.f('ix_catalog_entities_slug'), table_name='catalog_entities')
    op.drop_index(op.f('ix_catalog_entities_publisher'), table_name='catalog_entities')
    op.drop_index(op.f('ix_catalog_entities_kind'), table_name='catalog_entities')
    op.drop_table('catalog_entities')
    op.drop_index(op.f('ix_audit_events_request_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_occurred_at'), table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index(op.f('ix_control_authority_proposals_created_at'), table_name='control_authority_proposals')
    op.drop_index(op.f('ix_control_authority_proposals_applied_revision'), table_name='control_authority_proposals')
    op.drop_index(op.f('ix_control_authority_proposals_base_revision'), table_name='control_authority_proposals')
    op.drop_table('control_authority_proposals')
    op.drop_table('control_authority_heads')
    op.drop_index(op.f('ix_control_authority_revisions_created_at'), table_name='control_authority_revisions')
    op.drop_index(op.f('ix_control_authority_revisions_parent_revision'), table_name='control_authority_revisions')
    op.drop_table('control_authority_revisions')
    op.drop_table('agent_nodes')
    op.drop_index(op.f('ix_agent_issued_certificate_revocations_state'), table_name='agent_issued_certificate_revocations')
    op.drop_index(op.f('ix_agent_issued_certificate_revocations_node_id'), table_name='agent_issued_certificate_revocations')
    op.drop_table('agent_issued_certificate_revocations')
    op.drop_index(op.f('ix_agent_enrollment_grants_node_id'), table_name='agent_enrollment_grants')
    op.drop_index(op.f('ix_agent_enrollment_grants_expires_at'), table_name='agent_enrollment_grants')
    op.drop_index(op.f('ix_agent_enrollment_grants_created_at'), table_name='agent_enrollment_grants')
    op.drop_table('agent_enrollment_grants')
    # ### end Alembic commands ###

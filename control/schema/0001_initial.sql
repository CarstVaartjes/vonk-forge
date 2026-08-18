BEGIN;

CREATE TABLE agent_nodes (
    node_id TEXT PRIMARY KEY,
    identity TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    CHECK (state IN ('pending', 'active', 'revoked')),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE TABLE agent_profiles (
    node_id TEXT PRIMARY KEY REFERENCES agent_nodes(node_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    hostname TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'managed',
    labels JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (lifecycle IN ('managed', 'unmanaged')),
    CHECK (jsonb_typeof(labels) = 'object')
);

CREATE TABLE enrollment_intents (
    intent_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'created',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    token_verifier TEXT NOT NULL,
    consumed_at TIMESTAMPTZ,
    controller_endpoint TEXT NOT NULL,
    enrollment_endpoint TEXT NOT NULL,
    ca_fingerprint TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (state IN ('created', 'waiting_for_registration', 'pending_review', 'approved', 'rejected', 'expired', 'certificate_issued')),
    CHECK (jsonb_typeof(metadata) = 'object'),
    UNIQUE (intent_id, node_id)
);
CREATE INDEX ix_enrollment_intents_node ON enrollment_intents(node_id);

CREATE TABLE enrollment_evidence (
    evidence_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    csr_pem TEXT NOT NULL,
    host_identity TEXT NOT NULL,
    hardware_identity TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (intent_id),
    FOREIGN KEY (intent_id, node_id)
        REFERENCES enrollment_intents(intent_id, node_id) ON DELETE CASCADE,
    CHECK (jsonb_typeof(evidence) = 'object')
);

CREATE TABLE certificate_records (
    certificate_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE RESTRICT,
    intent_id TEXT REFERENCES enrollment_intents(intent_id) ON DELETE RESTRICT,
    certificate_identity TEXT NOT NULL,
    serial_number TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    not_after TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    UNIQUE (serial_number),
    UNIQUE (fingerprint),
    CHECK (state IN ('active', 'revoked', 'expired')),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_active_certificate_identity ON certificate_records(certificate_identity) WHERE state = 'active';

CREATE TABLE presence_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE CASCADE,
    certificate_id TEXT REFERENCES certificate_records(certificate_id) ON DELETE SET NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (jsonb_typeof(details) = 'object')
);
CREATE INDEX ix_presence_snapshots_node_time ON presence_snapshots(node_id, observed_at DESC);

CREATE TABLE telemetry_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (jsonb_typeof(metrics) = 'object')
);
CREATE INDEX ix_telemetry_snapshots_node_time ON telemetry_snapshots(node_id, observed_at DESC);

CREATE TABLE inventory_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    inventory JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (jsonb_typeof(inventory) = 'object')
);
CREATE INDEX ix_inventory_snapshots_node_time ON inventory_snapshots(node_id, observed_at DESC);

CREATE TABLE recipes (
    recipe_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE recipe_revisions (
    revision_id TEXT PRIMARY KEY,
    recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    content JSONB NOT NULL,
    content_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (recipe_id, revision_number),
    UNIQUE (recipe_id, content_digest),
    CHECK (revision_number >= 1),
    CHECK (jsonb_typeof(content) = 'object'),
    CHECK (content_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE recipe_import_reports (
    report_id TEXT PRIMARY KEY,
    recipe_id TEXT REFERENCES recipes(recipe_id) ON DELETE SET NULL,
    source_reference TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    report JSONB NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_reference, source_digest),
    CHECK (jsonb_typeof(report) = 'object')
);

CREATE TABLE placements (
    placement_id TEXT PRIMARY KEY,
    recipe_revision_id TEXT NOT NULL REFERENCES recipe_revisions(revision_id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE RESTRICT,
    rank INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'planned',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (recipe_revision_id, node_id),
    CHECK (rank >= 0),
    CHECK (state IN ('planned', 'ready', 'removed'))
);

CREATE TABLE installations (
    installation_id TEXT PRIMARY KEY,
    placement_id TEXT NOT NULL REFERENCES placements(placement_id) ON DELETE RESTRICT,
    recipe_revision_id TEXT NOT NULL REFERENCES recipe_revisions(revision_id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'planned',
    digest TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (state IN ('planned', 'installing', 'installed', 'failed', 'uninstalled'))
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL REFERENCES agent_nodes(node_id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'planned',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (state IN ('planned', 'running', 'stopped', 'failed'))
);

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    node_id TEXT REFERENCES agent_nodes(node_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (owner_kind IN ('installation', 'run', 'recipe', 'node')),
    CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    UNIQUE (owner_kind, owner_id, operation_id)
);
CREATE INDEX ix_operations_owner ON operations(owner_kind, owner_id);

-- A grant is represented by an intent; at most one unconsumed grant exists per intent.
CREATE UNIQUE INDEX uq_active_grant_per_intent
    ON enrollment_intents(intent_id)
    WHERE consumed_at IS NULL AND state IN ('created', 'waiting_for_registration');

COMMIT;

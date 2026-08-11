# Generic fleet migration

`vonk-forge` supports two inventory formats during the N-node migration:

- `inventory/cluster.toml` is the original as-built, two-GPU node record. It
  remains read-only and continues to support the accepted runtime definitions.
- `inventory/fleet.toml` is the generic version 2 format used by newly
  onboarded nodes. It has no fixed node count or fixed node names.

The version 2 inventory is now the controller's authoritative fleet input.
`cluster.toml` remains only as accepted legacy evidence for older runtime and
fabric tooling; new controller reads and node onboarding must use `fleet.toml`.

## Identity model

Generic records use immutable IDs formatted as `spk_` followed by 32 lowercase
hexadecimal characters. A node's ID does not change when its display name,
hostname, management address, SSH alias, role, or topology changes.

The compatibility reader assigns deterministic UUIDv5-based IDs to legacy host
keys. The namespace is scoped to the canonical `vonk-forge` repository identity,
and the legacy table key is the only per-node input. Consequently, an address
or hostname correction does not change compatibility identity.

These IDs adapt existing evidence; they are not proof of physical identity.
Fresh onboarding uses a generated ID and a separate trusted hardware/host-key
identity gate.

## Inspect the current legacy inventory

This command loads the current file through the compatibility reader and prints
only sanitized fields:

```bash
uv run python - <<'PY'
from pathlib import Path
from cluster_profiles.fleet.legacy import load_legacy_cluster

fleet = load_legacy_cluster(Path("inventory/cluster.toml"))
for node in fleet.ready_nodes():
    print(node.id.value, node.display_name, node.hostname, node.management.host)
PY
```

The reader has no write function. Verify that inspection left the source
unchanged with `git diff -- inventory/cluster.toml`.

## Validate a generic inventory

A generic document has this shape:

```toml
schema_version = 2

[nodes.spk_0123456789abcdef0123456789abcdef]
display_name = "studio-a"
hostname = "node-studio-a"
lifecycle = "ready"

[nodes.spk_0123456789abcdef0123456789abcdef.management]
host = "node-studio-a.local"
user = "operator"
port = 22
credential_ref = "secret://ssh/vonk-admin"

[nodes.spk_0123456789abcdef0123456789abcdef.labels]
site = "studio"
```

Load it without resolving DNS or opening SSH:

```bash
uv run python - <<'PY'
from pathlib import Path
from cluster_profiles.fleet.loaders import load_fleet

fleet = load_fleet(Path("inventory/fleet.toml"))
print(f"validated {len(fleet.nodes)} nodes")
PY
```

The repository and packaged JSON contracts are
`schemas/fleet.schema.json` and `schemas/topology.schema.json`. Unknown fields,
embedded credential values, malformed IDs, duplicate display names, invalid
topology references, and unsupported schema versions fail closed.

## Migration and rollback rules

Generic serialization is a deliberate one-way operation. Do not generate and
commit `inventory/fleet.toml` from the compatibility reader merely because the
legacy file parses. A generic record becomes authoritative only after the
per-node onboarding workflow has passed its identity, inventory, access,
hardening, policy, and acceptance gates.

For every accepted node, commit the certificate-bound `spk_` identity and its
sanitized management metadata to `inventory/fleet.toml`; commit physical links
to `inventory/topology.json`. Never copy a private key, token, password, raw
serial, or mutable observation into either document.

The controller does not fall back to `inventory/cluster.toml`. A deployment
commit missing `fleet.toml` is incomplete and fleet-dependent API operations
fail closed. Roll back by selecting an earlier complete repository commit with
its matching fleet and topology documents, never by synthesizing IDs from the
legacy file after pairing.

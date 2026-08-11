# Migrate a Python GPU node agent to Rust

This is a credential replacement, not a private-key copy. The Rust agent
creates a new Ed25519 key locally. A dedicated, short-lived migration grant
issues certificate generation N+1 for the existing node. The first valid Rust
protocol-3 claim atomically marks migration complete and locally revokes all
older certificates.

The Python service remains the test oracle for one release. Do not remove its
files or units until the physical GPU node soak is accepted.

## Preconditions

1. The controller shows an active Python node with `Migration required`.
2. No operation is running or waiting for delivery. Resolve or acknowledge it
   with Python first; the receipt importer rejects active work.
3. Back up the Python state and `/etc` configuration. Never copy its private
   key into `/var/lib/vonk-forge/agent`.
4. Install `vonk-forge-agent` and configure both the `enrollment_url` used
   only for pairing and the post-certificate `controller_url`, plus the CA pin
   and the same node ID, using the installation runbook.

## Cut over

Stop the legacy unit and snapshot only its receipt database. Replace the unit
and source path below if the legacy installation used different names:

```bash
sudo systemctl stop vonk-agent.service
sudo install -o vonk-agent -g vonk-agent -m 0400 \
  /var/lib/vonk-forge/agent/agent-state.sqlite3 \
  /var/lib/vonk-forge/agent/python-receipts.sqlite3
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent \
  migrate-python-state \
  --source /var/lib/vonk-forge/agent/python-receipts.sqlite3
```

The import is atomic and idempotent. It validates the exact Python v1 schema,
canonical result bytes, node/job/operation/attempt/fence binding, terminal
state, and acknowledgement state. It reads no credential table or file.

From the admin interface create a **Rust migration grant** for this node. Pair
as `vonk-agent`, approve the pending enrollment, and repeat pairing to collect
the fresh certificate:

```bash
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent pair \
  --enrollment https://enroll.example.internal/ \
  --ca-sha256 REPLACE_WITH_64_LOWERCASE_HEX \
  --token-stdin < /run/secrets/vonk-migration-token
```

Start the new service and wait for the controller to show `Rust agent` and
`complete`:

```bash
sudo systemctl enable --now vonk-agent-helper.socket
sudo systemctl enable --now vonk-agent-supervisor.service
sudo systemctl status vonk-agent.service
```

Only then disable the legacy service and delete the token and staged receipt
copy:

```bash
sudo systemctl disable vonk-agent.service
sudo rm -f /run/secrets/vonk-migration-token
sudo rm -f /var/lib/vonk-forge/agent/python-receipts.sqlite3
```

Keep the legacy package and backed-up state through the soak window. If Rust
does not authenticate, stop the Rust supervisor and restart Python; because no
Rust claim succeeded, the old certificate remains active. After a successful
Rust claim the old certificate is deliberately revoked and rollback requires
an administrator-issued recovery identity, not reuse of the old key.

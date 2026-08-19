# Fleet onboarding handover

Date: 20260819
Status: Implementation closed; deployment acceptance pending

## Summary

The control-plane Fleet **Add Spark** implementation is closed in source. The
grant contract, displayed command, bootstrap defaults, and deployment trust
binding now agree. This handover does **not** claim that the change has been
deployed or accepted against a real Spark.

## Closed implementation

### 1. The grant contract allows the Fleet lifetime

The enrollment grant maximum is the shared
`MAX_ENROLLMENT_GRANT_TTL_SECONDS` value of **900 seconds**. The control API,
enrollment services, OpenAPI document, and Fleet UI all use that contract; the
Fleet UI requests the 900-second maximum rather than a value rejected by the
API.

### 2. The UI preserves actionable API validation errors

The web client formats structured FastAPI validation details into bounded,
human-readable text. It no longer displays a nested validation object as
`[object Object]`.

### 3. The generated command has the four required values

The Fleet UI emits a shell-quoted, one-time command containing exactly the
grant token, controller endpoint, enrollment endpoint, and CA fingerprint:

```text
sudo /var/lib/vonk-forge/supervisor/current/vonk-agent bootstrap \
  --token '<token>' \
  --controller-endpoint '<controller-origin>' \
  --enrollment-endpoint '<enrollment-origin>' \
  --ca-fingerprint '<64-lowercase-hex>'
```

The shipped Rust bootstrap command supplies the canonical paths; the operator
does not pass local path flags:

- configuration: `/etc/vonk-forge-agent/agent.toml`
- data directory: `/var/lib/vonk-forge-agent`
- controller CA: `/etc/vonk-forge-agent/controller-ca.pem`

Bootstrap replaces only the packaged placeholder, generates the canonical
node identity locally, and never writes the grant token to `agent.toml`. A
matching valid configuration is preserved; conflicting local values fail
closed. The same command drives the existing Rust pairing path before and
after approval.

### 4. The grant binds to configured deployment trust

At control-API startup, the enrollment response derives its CA fingerprint
from the single PEM certificate configured by `VONK_CONTROLLER_CA_FILE`; it is
the lowercase SHA-256 fingerprint of that certificate, not a placeholder. The
controller and enrollment endpoints are likewise the fixed HTTPS origins
configured by `VONK_AGENT_CONTROLLER_ORIGIN` and
`VONK_AGENT_ENROLLMENT_ORIGIN`. Invalid origins, a missing CA path, a symlink,
or a non-single-certificate PEM fail closed.

The controller and enrollment origins are separate HTTPS hostnames, but both
share the existing GPU-node-restricted Caddy listener on TCP 8443; this change
does not introduce or expose a new port.

## Deployment handoff and verification status

Rebuild and redeploy the control API, Caddy configuration, and web bundle. Also
rebuild and roll out the agent package because the shipped Rust executable now
owns bootstrap and the `agent.toml` materialization contract.

Deployment acceptance is **not performed** for this handover, and there is no
real-Spark evidence yet. Do not mark this work deployed or accepted until the
following sequence succeeds against the deployed revision:

1. Create a Fleet grant.
2. Run the generated command on a Spark.
3. Approve the pending enrollment.
4. Repeat the bootstrap request according to the existing idempotency contract.
5. Confirm the authenticated node appears live in Fleet.

## Closure criteria

- The Add Spark action creates a 900-second grant without HTTP 422.
- API validation errors display actionable text, never `[object Object]`.
- The command invokes the shipped Rust executable, contains the four grant
  values, and bootstrap uses the canonical `agent.toml` path defaults.
- The grant uses the configured controller-CA fingerprint.
- Deployment acceptance remains pending until the real-Spark sequence above is
  evidenced.

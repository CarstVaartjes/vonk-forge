# Task 7 report: exact development Tailscale Service gateway

## Status

Implemented the structurally complete development-only Tailscale gateway and
configurator, exact one-Service reconciler, and non-secret Service-hostname
handoff. No Task 8 caller, renderer, workflow, or end-to-end stack file was
modified.

## Exact final development map

```json
{"services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}},"version":"0.0.1"}
```

The only advertised development Service is `svc:vonk-forge`; its only
endpoint is HTTPS 443 to `http://caddy:8080`. Reconciliation resets any extra,
plaintext, TCP-forwarded, or retargeted state before applying and advertising
that map. There is no Funnel, HTTP listener, TCP 22 listener, host port 8080,
TUN device, `NET_ADMIN`, Docker socket, or public browser listener.

## Hostname handoff and startup dependency graph

The selected handoff is a non-secret named volume,
`dev-tailscale-runtime`, containing one atomically replaced mode-0444 file:

```text
/run/vonk-tailnet/control-hostname
```

The configurator reads `CurrentTailnet.MagicDNSSuffix` from live
`tailscale status --json`, validates a `.ts.net` DNS suffix, derives exactly
`vonk-forge.<tailnet-name>.ts.net`, publishes the file, and prints only the
corresponding non-secret `https://.../` operational URL.

```text
control-api healthy + dev-init complete
                 |
                 v
       Caddy agent-only stage healthy       Tailscale gateway healthy
       (no :8080 browser listener)                    |
                 |                                    |
                 +----------> configurator <----------+
                                  |
                    exact map + service-host verified
                                  |
                    atomic hostname-file publication
                                  |
                    Caddy watcher restarts/revalidates
                                  |
                    exact Host browser edge on :8080
                                  |
                    canonical /healthz then steady
                    60-second reconciliation
```

There is no dependency cycle: the gateway has no application dependency;
Caddy has no Tailscale dependency; the configurator waits for healthy Caddy
and gateway. Before hostname discovery Caddy removes the bounded browser block
between exact markers and serves only its existing loopback health and agent
TLS sites. Thus a tailnet/OAuth outage leaves API, worker, inference, and agent
ingress independently available. Browser readiness remains represented by the
configurator: it is not healthy until the exact map, `service-host`, hostname
file, and HTTPS status are present. Persisted hostname removal or replacement
causes Caddy to return to the staged path or reload the newly validated exact
Host; no placeholder Host is used.

## Least-privilege matrix

| Component | Network authority | Files / secrets | Runtime privilege | Published exposure |
| --- | --- | --- | --- | --- |
| `tailscale-gateway` | egress `ingress` plus internal `tailnet-web-edge` | OAuth ID/secret files, state volume, socket volume | official digest-pinned image, userspace, read-only root, `cap_drop: ALL`, no-new-privileges | none |
| `tailscale-configurator` | gateway network namespace only | socket, packaged runtime assets read-only, non-secret hostname volume read-write; no secrets | same pinned image, read-only root, `cap_drop: ALL`, no-new-privileges | none |
| Caddy | existing `application`/`ingress` plus internal `tailnet-web-edge` | existing Caddy projections plus hostname volume read-only | existing UID 10000 hardening unchanged | existing agent 8443 only; no browser LAN port |

The gateway advertises only `tag:vonk-gateway`, uses `TS_AUTH_ONCE=true`, a
persistent state directory and socket, and `TS_USERSPACE=true`. The existing
reviewed policy already has the exact administrator grant to
`svc:vonk-forge:443` and exact auto-approver
`["tag:vonk-gateway"]`; its Hermes grant remains separately scoped.

## Strict RED/GREEN evidence

Initial static RED:

```text
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py -q
6 failed, 67 warnings
```

Reconciler RED:

```text
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py -k reconciler -q
3 failed, 3 deselected, 67 warnings
```

Hostname-handoff RED was run against the selected configurator/resource cases
and produced `2 failed`; the Caddy file input test independently produced
`1 failed`. The honest-health regression first failed because the healthcheck
lacked `serve status --json`. The no-cycle topology RED produced two static
failures while Caddy still depended on the configurator, and the missing-file
staged-Caddy test produced one failure. The final persisted-hostname watcher
regression was demonstrated with:

```text
uv run --project control --frozen pytest control/tests/test_dev_runtime_assets.py::test_caddy_entrypoint_restarts_when_discovered_hostname_changes_or_disappears -q
1 failed
```

After the minimal implementation, focused GREEN was:

```text
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py -q
7 passed, 67 warnings

uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py deploy/compose/tests/test_tailscale.py -q
14 passed, 67 warnings

uv run --project control --frozen pytest control/tests/test_dev_runtime_assets.py -q
18 passed, 67 warnings in 2.51s

uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_agent_ingress.py -q
15 passed, 67 warnings in 12.05s

uv run --python 3.12 --frozen --with pytest==9.1.1 pytest scripts/tests/test_dev_runtime_secrets.py::test_declares_local_source_and_deployment_secret_boundaries -q
1 passed in 0.04s
```

The brief's full unmodified combined command was also run:

```text
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py deploy/compose/tests/test_tailscale.py deploy/compose/tests/test_dev_compose.py -q
32 passed, 16 failed, 67 warnings in 8.59s
```

All 16 failures originate in `test_dev_compose.py::_existing_secrets`: that
Task 8 caller invokes `scripts/dev-runtime-secrets.py` without the required
`--tailscale-oauth-client-id-file` and
`--tailscale-oauth-client-secret-file` arguments. The failures were neither
weakened nor skipped, and Task 8 caller files were not modified.

## Shell, image, YAML, and diff checks

The following completed with exit 0 and no diagnostics:

```text
/bin/sh -n control/src/vonk_control/resources/dev/tailscale-configure.sh
/bin/sh -n control/src/vonk_control/resources/dev/caddy-entrypoint.sh
docker compose -f deploy/compose/compose.dev.images.yaml config --quiet
git diff --check
```

The exact pinned Tailscale image was executed and confirmed to contain every
runtime command used by the configurator: `tailscale`, `sed`, `grep`, `tr`,
`head`, `wget`, `sleep`, `chmod`, `mv`, and `rm`.

## Production-preservation proof

Production's existing map remains exactly:

```text
svc:vonk-forge       HTTPS 443 -> http://caddy:8080
svc:hermes-api       HTTPS 443 -> http://hermes-agent:8642
svc:hermes-dashboard HTTPS 443 -> http://hermes-agent:9119
```

The new production-preservation assertion requires exactly three `--service`
commands and three advertisements. The production gateway implementation and
policy were byte-unchanged from Task 6, proven by this exit-0/no-output check:

```text
git diff --exit-code HEAD -- deploy/compose/tailscale/compose.yaml deploy/compose/tailscale/configure.sh deploy/compose/tailscale/grants.example.hujson
```

## Secret-output audit

- The fake CLI passes sentinel OAuth environment values and asserts neither
  appears on stdout or stderr.
- The configurator source contains no OAuth variable names, Funnel, Docker
  socket, wildcard Service, Hermes Service, HTTP listener, or TCP listener.
- Only the gateway receives the two existing OAuth Compose projections; the
  configurator and Caddy receive no OAuth secret.
- The hostname file and printed URL are explicitly non-secret operational
  output derived from live Tailscale status; no real tailnet is hardcoded.
- No 22nd source secret was added. The focused boundary test still proves
  exactly 21 local source and 17 deployment secret names.
- `scripts/dev-runtime-secrets.py`, `scripts/dev-compose-secrets.py`, and
  `scripts/dev-compose` were
  unchanged, preserving the Task 8 ownership boundary.

## Self-review and concerns

Self-review found and fixed two issues before this report: full-mode Caddy now
watches persisted hostname replacement/removal, and the broader agent-ingress
test now records the intentionally added private Caddy network. Shell parsing,
rendered Compose, image tools, least privilege, hostname validation, startup
cycle, production map, and secret output were then rechecked.

The remaining concern is exclusively the 16 Task 8 secret-caller wiring
failures above. The recurring 67 pytest cleanup warnings are the existing
sealed runtime-secret fixture warnings recorded by prior tasks; no Task 7 test
was skipped or masked.

## Fix round 1/5: fresh continuous hostname authority

The hostname handoff now separates the stable non-secret operational output
`control-hostname` from generation-scoped Caddy authority
`control-hostname.ready`, whose exact record is `<configurator UUID>
vonk-forge.<live MagicDNSSuffix>`. Caddy always starts agent-only and snapshots
persisted authority; only a subsequent atomic publication from the current
configurator generation enables the browser edge. Replacement carries fresh
authority directly into a validated re-exec, while removal or malformed input
returns Caddy to agent-only. This preserves the acyclic dependency graph:
gateway -> configurator live validation -> atomic outputs -> Caddy watcher;
the configurator still depends on independently healthy gateway and staged
Caddy, and Caddy never depends on the configurator.

Strict executable RED evidence:

```text
uv run --project control --frozen pytest control/tests/test_dev_runtime_assets.py::test_caddy_entrypoint_requires_fresh_generation_and_reacts_to_real_file_events -q
1 failed, 67 warnings in 0.43s
Failure: persisted vonk-forge.yesterdays-tailnet.ts.net enabled browser mode instead of agent-only.

uv run --python 3.12 --frozen --with pytest==9.1.1 pytest deploy/compose/tests/test_dev_tailscale.py::test_continuous_reconciler_republishes_live_suffix_and_health_rejects_stale -q
1 failed, 67 warnings in 8.08s
Failure: no generation-scoped control-hostname.ready record was published.
```

Focused GREEN evidence:

```text
deploy/compose/tests/test_dev_tailscale.py + deploy/compose/tests/test_tailscale.py: 15 passed
control/tests/test_dev_runtime_assets.py: 18 passed
deploy/compose/tests/test_agent_ingress.py: 15 passed
Total: 48 passed, 0 failed
```

The continuous fake CLI changes `MagicDNSSuffix`, observes only complete old or
new atomic records, proves Caddy reacts to real replacement/removal events, and
proves health fails while live status and published authority disagree. Health
now validates the current generation, exact live suffix, operational hostname,
ready record, exact HTTPS Service map, `service-host`, and canonical Caddy
health. Production reconciliation remains 60 seconds; one-second controls are
accepted only with explicit test mode. The transient watcher failure during
GREEN was traced to derived `VONK_CONTROL_HOSTNAME` surviving re-exec and
colliding with file authority; clearing that derived value before revalidation
resolved it.

The final development map remains exactly `svc:vonk-forge` HTTPS 443 ->
`http://caddy:8080`. No secret, OAuth scope, host port, Funnel, TUN device,
capability, Docker socket, or public exposure was added. Production gateway,
policy, and three-Service map remain byte-unchanged. Shell syntax, Compose
rendering, `git diff --check`, production-preservation diff, and the 21-source /
17-deployment secret boundary check all exited 0. The only outstanding failures
remain the 16 unmodified Task 8 caller-wiring cases already recorded above.

# Operator CLI access

Use the execution host's authenticated CLIs and HTTP APIs for supported
operations. Read installed command help and, if present,
`~/.config/cli-access/README.md` before using host-specific credentials or
helpers. These local helpers are operator configuration, not commands shipped
by the Vonk installer; do not assume another host has them.

## Choose the authorized interface

| Operation | Interface |
| --- | --- |
| GitHub and HTTPS Git | `gh` and its Git credential helper |
| Local tailnet connection | `tailscale` |
| Tailnet administration | Configured scoped API credential; `tailscale-api` where installed |
| Automation secrets | Scoped service account; `op-headless` where installed |
| Controller and Spark operations | `vonkctl` |
| NAS Compose project | Docker Compose through approved host access |
| Diagnostic/bootstrap host access | Configured SSH credentials; `vonk-ssh` where installed |

Do not launch a browser, personal password-manager session, or desktop unlock
for an operation these paths support. A denied service-account read remains
denied: do not widen its vault access or silently retry through a personal
account. An explicitly authorized one-time provisioning step is separate from
routine operation. Keep tokens, passwords, private keys and resolved Compose
secrets out of arguments, output, tracked files and PRs.

If a bounded CLI check fails, identify the actual boundary: sandbox network or
socket access, connectivity, authentication, or service authorization. When
policy permits, repeat the same narrow check with approved execution access
before declaring login broken. Do not change sandbox policy or bypass a real
permission refusal. `cli-access-check`, where installed, checks current access;
its success does not authorize every operation or prove every permission.

GitHub API authentication, Git signing, and SSH authentication are separate.
Keep commit signing enabled; use an approved machine signer when unattended
signing is required. Never export a personal private key to avoid an unlock.
For repository edits, first create the task's branch/worktree under the
[development workflow](development-workflow.md#start-isolated-work).

## NAS Compose redeployment

NAS host deployment is distinct from Controller-managed Spark rollout. An
explicitly authorized NAS Compose redeploy may use a configured headless SSH
transport and existing administrative credentials. Once the operation is
authorized, choosing that transport needs no additional approval merely because
the older workflow used a NAS browser. Normal approval controls remain in force.
The exception does not authorize SSH Spark rollouts, new privilege grants,
unrelated projects, or bypassing a Controller decision.

Before applying a release, inspect its accepted signed schema-2 manifest and
verify the intended build commit and exact artifacts. Prepare the existing
bundle with the signed NAS installer, preserve `.env`, `secrets/`, and named
volumes, and make the bound images available to the NAS. A floating image tag or
a newly published image alone is not accepted-release evidence. Follow the
[publication procedure](platform-release-publication.md) and
[control-plane bootstrap](control-plane-bootstrap.md) for those stages.

On a host with the configured `vonk-nas-compose` helper:

```sh
vonk-nas-compose status
vonk-nas-compose plan
vonk-nas-compose redeploy --release-json /path/to/release.json --release-signature /path/to/release.sig
```

`plan` uses Compose dry-run mode. `redeploy` applies the existing configured
project with `up --detach --no-build --pull never --wait --wait-timeout 180`.
It does not select a release, download images, or force unchanged containers
to restart. It leaves the installed Compose channel policy intact and separates
image preparation from application by overriding pulls for this invocation.
Required images must already be prepared; the host's local guide
owns its exact project name and paths. Do not guess these values or apply the
command to another project. Never use `down -v` or renew volumes for an ordinary
upgrade. A timeout requires inspecting actual state before deciding to retry;
it does not prove the deployment stopped or rolled back.

After application, verify container health, Controller version/readiness and
fleet state. The configured helper captures deployment provenance after Compose
succeeds. It passes the reviewed release and minimal Docker identity fields to
the packaged `vonk_control.capture_deployment_observation` module. The module
verifies the existing installer signature, running container and immutable image
digest against the accepted release, then records the Controller observation in
the `deployment-observations` named volume. It never consumes Docker environment
variables. The release signature input is its existing base64 text; only the raw
release JSON needs base64 encoding for the collector's stdin contract.

The observation is bound to the actual container hostname/ID and embedded source
commit. Its original capture time remains visible; a matching active instance
retains current identity evidence, while a replaced container requires capture
again. If capture fails after Compose succeeds, deployment and capture have
different outcomes. Inspect health and retry only the observation with
`vonk-nas-compose capture-provenance --release-json /path/to/release.json
--release-signature /path/to/release.sig`; do not infer rollback or reapply the
whole project merely because the observation response was lost.

Keep dry-run, deployment, and physical Spark acceptance separate.
Routine Spark package upgrades continue through `vonkctl fleet upgrade` and its
Controller authorization, not through the NAS host session.

## Renewal and availability evidence

Use approved credential lifetimes and unattended renewal where supported. Do
not extend lifetimes or broaden grants to conceal an authentication dependency.
The configured Vonk token-renewal helper preserves the Controller's normal
lifetime, validates a new token before replacing the old one, and fails visibly
if its scoped source credential or Controller authorization is refused.

Verify a locked but awake user session separately from logout, sleep, shutdown,
or pre-login disk unlock. A user LaunchAgent does not establish pre-login
availability. Work while the operator's laptop is off needs an explicitly
selected always-on execution host and its own tested access.

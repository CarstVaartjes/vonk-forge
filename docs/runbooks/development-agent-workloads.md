# Development agent workload acceptance

This is the executable development acceptance path for one generic Docker NAS
and two Ubuntu 24.04 ARM64 GPU nodes. It covers installation, pairing,
inventory, source build, distribution, install, routing, real inference,
restart persistence, rank failure/recovery, and normal cleanup. Complete the
steps in order. A green unit test is not a substitute for the physical gates.

## Scope and placeholders

Define every placeholder before executing a command. Site values are data, not
defaults baked into Compose or a recipe.

| Placeholder | Meaning |
|---|---|
| `<REPOSITORY_CHECKOUT>` | Clean operator checkout of the accepted `main` commit. |
| `<ACCEPTED_MAIN_SHA>` | Exact 40-character commit that published the development cohort. |
| `<DOWNLOAD_DIRECTORY>` | Private local directory containing the accepted Compose workflow artifact. |
| `<LOCAL_STAGING_DIRECTORY>` | Private mode `0700` local directory used to generate secrets. |
| `<LOCAL_SECRETS_DIR>` | `<LOCAL_STAGING_DIRECTORY>/secrets`. |
| `<MOUNTED_NAS_PARENT>` | Mounted SMB parent that contains the NAS project directory. |
| `<NAS_SSH_TARGET>` | Operator SSH target for loopback forwarding only. |
| `<NAS_MANAGEMENT_IP>` | NAS address on the GPU-node management LAN. |
| `<NODE_MANAGEMENT_CIDR>` | Canonical CIDR allowed to reach agent ingress. |
| `<ENROLLMENT_HOSTNAME>` | TLS name used only by first-contact pairing. |
| `<CONTROLLER_HOSTNAME>` | mTLS name used by an enrolled agent. |
| `<REGISTRY_HOSTNAME>` | Reserved site registry TLS name in the generated certificate. |
| `<SPARK_1_SSH_TARGET>`, `<SPARK_2_SSH_TARGET>` | Operator SSH aliases for the two nodes. |
| `<SPARK_1_NODE_ID>`, `<SPARK_2_NODE_ID>` | Distinct `spk_` plus 32-lowercase-hex identities. |
| `<SPARK_1_MANAGEMENT_IP>`, `<SPARK_2_MANAGEMENT_IP>` | Management addresses observed by Caddy. |
| `<SPARK_1_FABRIC_IP>`, `<SPARK_2_FABRIC_IP>` | Direct-fabric addresses on one common non-management network. |
| `<LOCAL_API_PORT>` | Unused operator loopback port, normally `18080`. |
| `<LOCAL_INFERENCE_PORT>` | Unused operator loopback port, normally `14000`. |
| `<EVIDENCE_DIRECTORY>` | Private local `.state/development-acceptance/` directory. |

The current lab acceptance worksheet is intentionally separate from these
generic commands: NAS `192.168.1.231`; GPU nodes `dgx-spark-1` at
`192.168.1.211` and `dgx-spark-2` at `192.168.1.212`; shared direct fabric
`192.168.100.10/24` and `192.168.100.11/24`; enrollment
`enroll.vonk-forge.lan`; controller `agents.vonk-forge.lan`; registry
`registry.vonk-forge.lan`. Record the two assigned `spk_` identities after
pairing. This paragraph is an example, not a product default.

## Prerequisites and trust boundaries

Before mutation, verify the accepted commit and publication:

```bash
set -euo pipefail
cd '<REPOSITORY_CHECKOUT>'
test "$(git rev-parse HEAD)" = '<ACCEPTED_MAIN_SHA>'
git diff --exit-code
git diff --cached --exit-code
docker buildx imagetools inspect \
  ghcr.io/carstvaartjes/vonk-forge-api:dev
docker buildx imagetools inspect \
  ghcr.io/carstvaartjes/vonk-forge-worker:dev
docker buildx imagetools inspect \
  ghcr.io/carstvaartjes/spark-ds4@sha256:084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615
```

All three inspections must work anonymously. A private package is a blocker;
do not place a GHCR token on the NAS, a GPU node, in Compose, or in an image.
Development uses mutable `:dev` only for operator-selected pull/redeploy.
Production remains selected by the trusted host updater and immutable TUF
target; `:latest` is never production deployment authority.

Confirm both nodes report `aarch64`, Ubuntu `24.04`, NVIDIA GB10 compute
capability 12.1, rootless Podman, NVIDIA CDI, enough disk/memory, and an active
common direct fabric. Confirm the NAS is `linux/amd64`, Docker Compose is
available, and the project directory is empty or contains only the supported
two-item layout.

## PKI and NAS project

Follow [Development NAS installation](development-nas-installation.md) from a
private local filesystem. The supported publication commands are:

```bash
set -euo pipefail
cd '<REPOSITORY_CHECKOUT>'
install -d -m 0700 '<LOCAL_STAGING_DIRECTORY>'
scripts/dev-runtime-secrets.py \
  --secrets-dir '<LOCAL_SECRETS_DIR>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
scripts/dev-runtime-project \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir '<LOCAL_SECRETS_DIR>' \
  --destination '<MOUNTED_NAS_PARENT>/vonk-forge' \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
```

The generator creates 14 local source files: 13 protected secret/config files
plus the public `git-signing-key.pub`. The protected `controller-ca-key` is
required to validate and rotate the controller CA, so include all 14 local
source files in one encrypted 1Password generation or equivalent backup before
first deployment. `controller-ca-key` must not be copied to the NAS.
An existing 13-file local source from an earlier branch head is incomplete: the
missing private key cannot be reconstructed from `controller-ca`. Create and
back up a fresh 14-file generation, then use the coordinated PKI rotation below;
do not replace only the CA or server certificate.

`dev-runtime-project` validates the complete local generation and projects
exactly 12 deployment files into the NAS `secrets/` directory; it excludes both
`controller-ca-key` and `git-signing-key.pub`. The NAS project must contain only
`docker-compose.yml` and `secrets/`. Pull/redeploy it in the Docker UI and keep
every named volume. Successful one-shot cohort, initialization, and migration
containers are expected to exit; PostgreSQL, API, worker, Caddy, and LiteLLM
must then be healthy. Never print secret values while diagnosing them.

## /etc/hosts and firewall

Local DNS is optional. Install the same line on the NAS and each GPU node:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Use an editor or configuration manager that writes `/etc/hosts` atomically,
then perform read-only checks:

```bash
getent hosts '<ENROLLMENT_HOSTNAME>'
getent hosts '<CONTROLLER_HOSTNAME>'
getent hosts '<REGISTRY_HOSTNAME>'
```

Allow `<NODE_MANAGEMENT_CIDR>` to `<NAS_MANAGEMENT_IP>:8443`; reject all other
sources. The development control API and inference gateway remain NAS
loopback-only. Verify port 8443 is reachable from each GPU node, enrollment TLS
chains to `controller-ca`, and the post-enrollment hostname rejects clients
without an accepted certificate.

## Package installation and pairing

On each GPU node, follow the signed APT steps in
[`docs/operations/agent-package-release.md#install-the-dev-channel`](../operations/agent-package-release.md#install-the-dev-channel),
then install the Rust package:

```bash
sudo apt update
sudo apt install vonk-forge-agent
apt-cache policy vonk-forge-agent
```

Do not enable both `dev` and `stable`. Copy only the public `controller-ca` to
each node and independently record its public DER SHA-256 fingerprint:

```bash
openssl x509 -in '<LOCAL_SECRETS_DIR>/controller-ca' -outform DER | sha256sum
scp '<LOCAL_SECRETS_DIR>/controller-ca' '<SPARK_1_SSH_TARGET>:/tmp/controller-ca.pem'
scp '<LOCAL_SECRETS_DIR>/controller-ca' '<SPARK_2_SSH_TARGET>:/tmp/controller-ca.pem'
```

On each node, install that certificate as root and set the complete
`/etc/vonk-forge-agent/agent.toml` inputs from
[Install the Vonk Forge agent](../operations/install-vonk-agent.md):
`enrollment_url`, `controller_url`, `ca_path`, the DER `ca_sha256`, and that
node's unique `node_id`. Use `https://<ENROLLMENT_HOSTNAME>:8443/` for
enrollment and `https://<CONTROLLER_HOSTNAME>:8443/` for authenticated
controller traffic. Generate a non-secret candidate identity with
`printf 'spk_%s\n' "$(openssl rand -hex 16)"`, record it, and never reuse it on
another node.

Pair one node at a time in this strict order:

1. Create one one-use node pairing grant in the administrator interface.
2. Save its token directly to a private root-readable node file; do not display
   or paste it into a command.
3. Run `vonk-agent pair` with the configured `enrollment_url`, CA fingerprint,
   and `--token-stdin`.
4. Approve the pending enrollment after comparing the node, CSR, host-key,
   hardware, agent, and boot evidence.
5. Repeat the same `vonk-agent pair` command to collect the issued certificate,
   then remove the one-use token file.
6. Enable the package-helper socket and supervisor, and confirm the controller
   reports the certificate-bound `spk_` identity.

The exact pair command is documented in the installation guide. Hostnames and
IP addresses are observations; the certificate-bound `spk_` value is identity.

## Inventory preflight

Create private operator evidence and access files:

```bash
set -euo pipefail
cd '<REPOSITORY_CHECKOUT>'
install -d -m 0700 '<EVIDENCE_DIRECTORY>'
scripts/dev-admin-token \
  --output '<EVIDENCE_DIRECTORY>/admin-token' \
  --ttl-seconds 21600
test "$(stat -c '%a' '<EVIDENCE_DIRECTORY>/admin-token')" = 600
test "$(stat -c '%a' '<LOCAL_SECRETS_DIR>/litellm-master-key')" = 600
```

The helper prints only the token path. Open one long-lived SSH tunnel in a
separate terminal:

```bash
ssh -N \
  -L <LOCAL_API_PORT>:127.0.0.1:8080 \
  -L <LOCAL_INFERENCE_PORT>:127.0.0.1:4000 \
  '<NAS_SSH_TARGET>'
```

Use `http://127.0.0.1:<LOCAL_API_PORT>` as `--api-base` and
`http://127.0.0.1:<LOCAL_INFERENCE_PORT>` as `--inference-base`. Confirm the
fleet shows both exact node IDs online, Rust protocol 3, fresh inventory,
rootless runtime capability, and distinct management/fabric addresses before
starting a workload. Retain only bounded API output in the evidence directory.

## Synthetic lifecycle

Run the deterministic source-only fixture through every public API stage:

```bash
cd '<REPOSITORY_CHECKOUT>'
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase synthetic \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/synthetic-1.json'
```

The final states must include source verification, image build/distribution,
install, run, route publication, exact deterministic inference, stop, route
withdrawal, and uninstall. Restart the NAS stack and both agent supervisors,
wait for fresh inventory, then run the same phase with
`synthetic-2.json`. Existing immutable source/artifact content may be reused;
the accepted receipts and final normal cleanup must still be complete.

## Real single-node model

Create `model-qualification-input.json` from fresh read-only observations. It
contains no credentials: anonymous image platform/label/user/public-pull
results; accepted license IDs; both certificate-bound node IDs; architecture,
OS, GPU/compute/CUDA code, rootless Podman status, available memory/disk,
management CIDRs, active fabric CIDRs/bandwidth; and the exact artifact
IDs/revisions/SHA-256/byte counts from
`config/recipes/development/model-smoke-artifacts.json`. The later agent install
independently downloads within the byte budget and hashes every HTTP artifact;
the qualification document is not a substitute for that receipt.

Fail closed unless the DS4 package is anonymously public and every fact is
current, then qualify:

```bash
cd '<REPOSITORY_CHECKOUT>'
scripts/qualify-development-model \
  --evidence '<EVIDENCE_DIRECTORY>/model-qualification-input.json' \
  --output '<EVIDENCE_DIRECTORY>/model-qualification.json'
```

Run to the first successful inference and pause:

```bash
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase model-single \
  --qualification-file '<EVIDENCE_DIRECTORY>/model-qualification.json' \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/model-single.json' \
  --stop-after inference-ok
```

Now perform the restart actions in the next section. Resume with the identical
command and evidence path, omitting `--stop-after`; the runner proves route and
inference persistence, then stops, withdraws, and uninstalls normally.

## Real multi-node failure and recovery

Start both ranks and pause after inference through the sole entrypoint:

```bash
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase model-multinode \
  --qualification-file '<EVIDENCE_DIRECTORY>/model-qualification.json' \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_2_NODE_ID>' \
  --failure-node '<SPARK_2_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/model-multinode.json' \
  --stop-after inference-ok
```

Read the non-secret run ID from the acceptance evidence and stop only the
second rank's exact rootless managed container. Keep the Rust agent running so
this proves workload-rank failure rather than loss of agent presence:

```bash
RUN_ID="$(jq -r '.outputs.run_id' '<EVIDENCE_DIRECTORY>/model-multinode.json)"
test "$RUN_ID" != null
ssh dgx-spark-2 sudo -u vonk-agent env \
  HOME=/var/lib/vonk-forge-agent \
  XDG_DATA_HOME=/var/lib/vonk-forge-agent \
  XDG_RUNTIME_DIR=/run/vonk-forge-agent \
  CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf \
  podman stop "vonk-$RUN_ID"
```

The authenticated agent submits complete local run snapshots no more than ten
seconds apart while a managed run exists. Resume the exact runner command with:

```text
--stop-after route-withdrawn-after-failure
```

The resumed runner first records `--stop-after rank-failure-observed`, proves
both agents remain healthy, and then requires the route to disappear. Recover
the exact stopped rank without rebuilding or deleting its managed state:

```bash
ssh dgx-spark-2 sudo -u vonk-agent env \
  HOME=/var/lib/vonk-forge-agent \
  XDG_DATA_HOME=/var/lib/vonk-forge-agent \
  XDG_RUNTIME_DIR=/run/vonk-forge-agent \
  CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf \
  podman start "vonk-$RUN_ID"
```

Wait for its health endpoint and next authenticated snapshot, then resume with:

```text
--stop-after inference-recovered
```

This proves both ranks are fresh, the sole route is republished, and inference
works again. Do not stop an agent, remove a container, delete a cache/model
directory, replace an identity, or delete a named volume to simulate rank
failure.

For both model phases, the private qualification SHA-256, build and distribution
evidence, and per-node runtime artifact evidence are retained by the acceptance
runner in the phase evidence file. Keep the qualification document and phase
evidence private even though their recorded public artifact identities are
non-secret.

## Restart persistence

For the single-node checkpoint, restart the target agent supervisor and stop
then start the NAS Compose project in its UI. For the multi-node checkpoint,
restart both supervisors and the NAS project after recovered inference. Keep
all named volumes and do not pull a different cohort during this gate.

Wait until the same two `spk_` identities and fresh inventory return. Resume
the identical runner command and evidence file without `--stop-after`. It must
observe a changed fleet evidence digest, advanced agent freshness, the still
published route, and successful inference without rebuilding or redownloading
immutable content.

## Normal stop and uninstall

Every uninterrupted runner finishes with `stopped`, `route-withdrawn`, and
`uninstalled`. If an operator checkpoint or recoverable error interrupted a
phase, repair the external condition and rerun the identical command with the
same evidence file. Request IDs are derived from its acceptance ID, so replay
is idempotent.

Use normal API stop/uninstall and reference counting. Do not broadly delete
agent storage, model files, containers, NAS named volumes, or the repository
volume. Preserve immutable caches after refcounts reach zero unless a separate
reviewed garbage-collection operation selects them.

## Rollback and secret rotation

Normal development update is an unchanged mutable Compose file followed by
operator **Pull** and **Redeploy**. Keep named volumes. A pinned Compose artifact
is for explicit reproduction or the guarded, schema-compatible recovery in
[Development NAS installation](development-nas-installation.md#advanced-guarded-recovery);
it is not a second mutable channel. Database-incompatible rollback requires a
matching full-state restore.

Rotate the PostgreSQL password and database URL only as one coordinated pair.
Rotate Git signing authority with historical public-key retention. Rotate
agent/controller PKI and LiteLLM/proxy tokens as one planned new 14-file local
source generation: back it up, distribute replacement public trust first,
schedule re-enrollment/client key change, project the exact 12-file NAS bundle,
and pull/redeploy. Never overwrite one CA private key or one server certificate
in isolation and hope the other projections recover.

## Evidence and clean-room audit

All token and evidence files use mode `0600` under the private
`.state/development-acceptance/` directory. They must not be committed,
uploaded as Actions artifacts, pasted into issues, or copied into the NAS
project. Store only public fingerprints, accepted commit/image digests,
certificate-bound node IDs, bounded operation IDs, state transitions, and
response hashes in a report.

Before physical deployment, perform a disposable local clean-room pass:

```bash
set -euo pipefail
cd '<REPOSITORY_CHECKOUT>'
ACCEPTANCE_ROOT=$(mktemp -d)
chmod 0700 "$ACCEPTANCE_ROOT"
scripts/dev-runtime-secrets.py \
  --secrets-dir "$ACCEPTANCE_ROOT/secrets" \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
scripts/dev-runtime-project \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir "$ACCEPTANCE_ROOT/secrets" \
  --destination "$ACCEPTANCE_ROOT/project" \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
docker compose -f "$ACCEPTANCE_ROOT/project/docker-compose.yml" config --quiet
uv run --project control pytest \
  control/tests/test_development_recipe_fixture.py -q
```

Record the public fingerprints and test-state IDs, then move the disposable
directory to the workstation's encrypted trash. Never print secret values or
retain the generated bundle in test output.

## Temporary sudo cleanup

Temporary unattended sudo is permitted only for the bounded acceptance window.
After every phase, normal stop/uninstall, and final read-only audit succeeds,
run this on the NAS and both GPU nodes:

```bash
sudo rm -f /etc/sudoers.d/vonktemp \
  /etc/sudoers.d/99-vonk-codex-temporary
sudo -k
if sudo -n true 2>/dev/null; then
  exit 1
else
  echo PASSWORD_REQUIRED
fi
```

`PASSWORD_REQUIRED` is the success result. Disable NAS SSH in its UI afterward
when that is the site's normal posture. Do not declare the physical acceptance
complete while either temporary sudo file remains.

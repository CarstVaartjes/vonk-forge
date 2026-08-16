# Development agent workload acceptance

This is the executable development acceptance path for one generic Docker NAS
and two Ubuntu 24.04 ARM64 GPU nodes. It covers installation, pairing,
inventory, source build, distribution, install, routing, real inference,
restart persistence, rank failure/recovery, and normal cleanup. Complete the
steps in order. A green unit test is not a substitute for the physical gates.

A fresh pre-production reset removes every user, browser session, and agent
enrollment. Recreate the development administrator, sign in to establish a
fresh browser session, and re-enroll every Spark before acceptance. Never use a
pre-reset login, token, session, or enrollment as physical acceptance evidence.

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
| `<LOCAL_OAUTH_INPUT_DIRECTORY>` | Private mode `0700` local directory containing the separately captured Tailscale OAuth client ID and secret files. |
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
  ghcr.io/carstvaartjes/vonk-forge-workloads@sha256:96993dcbb8f262c6fbcc41fd005498934b476b040486a6618898d4135b6d0817
```

All three inspections must work anonymously. A private package is a blocker;
do not place a GHCR token on the NAS, a GPU node, in Compose, or in an image.
Development uses mutable `:dev` only for operator-selected pull/redeploy.
Production remains selected by the trusted host updater and immutable TUF
target; `:latest` is never production deployment authority.

Confirm both nodes report `aarch64`, Ubuntu `24.04`, NVIDIA GB10 compute
capability 12.1, rootless Podman build isolation with at least 65,536
subordinate UIDs and GIDs for `vonk-agent`, Spark-managed Docker with NVIDIA
CDI `nvidia.com/gpu=all`, enough disk/memory, and an active common direct TCP
fabric. Confirm the NAS is `linux/amd64`, Docker Compose is available,
and the project directory is empty or contains only the supported two-item
layout.

The packaged builder keeps every temporary Podman graph below the
mode-`0700` `/var/lib/vonk-forge-agent` ancestor and uses
`overlay.force_mask=shared` with `/usr/bin/fuse-overlayfs`. The force mask lets
the parent agent traverse subordinate-UID image directories and account for
every byte; `fuse-overlayfs` presents the image's original permissions inside
the build container. These settings are one boundary: do not remove the force
mask, make the private ancestor traversable by other host users, or ignore
permission errors during storage accounting.

The package also enables a lingering `vonk-agent` user manager. Podman uses
that account's read-only user D-Bus endpoint and the systemd cgroup manager;
the service keeps `ProtectControlGroups=yes`. It deliberately sets
`ProtectHostname=no` because rootless `runc` must set the hostname inside each
build's private UTS namespace. The dedicated service user has no ambient
hostname capability, so this does not authorize changing the host hostname.
`AF_NETLINK` permits `runc` to create the isolated namespace but does not grant
build egress: accepted source builds still require `--network=none`. Treat a
missing user bus, `Linger=no`, or a Podman cgroup fallback as a failed node
preflight.

## PKI and NAS project

Follow [Development NAS installation](development-nas-installation.md) from a
private local filesystem. The supported publication commands are:

```bash
set -euo pipefail
cd '<REPOSITORY_CHECKOUT>'
install -d -m 0700 '<LOCAL_STAGING_DIRECTORY>'
uv run --project control --frozen scripts/dev-runtime-secrets.py \
  --secrets-dir '<LOCAL_SECRETS_DIR>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>' \
  --tailscale-oauth-client-id-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-id' \
  --tailscale-oauth-client-secret-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-secret'
uv run --project control --frozen scripts/dev-runtime-project-remote \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir '<LOCAL_SECRETS_DIR>' \
  --ssh-target '<NAS_SSH_TARGET>' \
  --identity-file '<ABSOLUTE_SSH_IDENTITY_FILE>' \
  --remote-destination '<NAS_LINUX_DOCKER_PARENT>/vonk-forge' \
  --docker-mode sudo \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --direct-fabric-cidrs '<DIRECT_FABRIC_CIDRS_OR_NONE>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
```

The generator creates exactly 22 local source files: exactly 18 deployment
files plus four local-only files: `admin-password`, `controller-ca-key`,
`git-signing-key.pub`, and `host-runtime-grant-public-key`. The protected local
source preserves the administrator, controller, Git-signing, and host-runtime
authorities, so include all 22 files in one encrypted 1Password generation or
equivalent backup before first deployment. None of the four local-only files
is copied to the NAS; in particular, the plaintext `admin-password` stays only
in the local generation, its encrypted backup, and the named 1Password item.
For two-node acceptance, pass the canonical NVIDIA Sync direct networks and
configure each agent with one address from those networks plus its measured
bandwidth. The publisher rejects any direct network that overlaps management.
For the one supported host-authority upgrade from the original 15-file source
generation, rerun the same command once with
`--upgrade-host-runtime-authority`. The helper
accepts only an otherwise complete and valid legacy generation, performs an
add-only migration by adding the two
host-runtime authority files, leaves every existing file byte-for-byte
unchanged, and can recover if power is lost after publishing the private half.
That produces the valid pre-browser 17-file source generation. Rerun the full
command with both OAuth input files and `--upgrade-browser-access`; this
add-only browser migration preserves all 17 existing bytes and adds the four
browser files. Then run `--upgrade-litellm-key-management` to add only
`litellm-database-password`. Back up the resulting 22-file generation
before deployment. It
rejects a public-only key, unknown file, inconsistent generation, or ordinary
incomplete directory; do not work around that refusal by replacing the CA or
server certificate.

`dev-runtime-project` validates the complete local generation and projects
exactly 18 deployment files into the NAS `secrets/` directory; it excludes
`admin-password`, `controller-ca-key`, `git-signing-key.pub`, and
`host-runtime-grant-public-key`. The NAS project must contain only
`docker-compose.yaml` and `secrets/`. Choose **Pull** then **Redeploy** in the
Docker UI and keep every named volume. Successful one-shot cohort,
initialization, and migration containers are expected to exit; PostgreSQL,
API, worker, Caddy, and LiteLLM must then be healthy. Never print secret values
while diagnosing them.

A development pull/redeploy replaces the single `control-api` replica. During
that bounded interval, in-flight agent requests may produce Caddy `EOF`,
`lookup control-api ... no such host`, and HTTP 502 log entries. Agents retry;
these entries are not by themselves a DNS defect. Before any workload action,
require the replacement API to be ready, Docker DNS to resolve its current
address, both certificate-bound agents to report fresh inventory, and no new
Caddy proxy errors after readiness:

```bash
cd '<NAS_PROJECT_DIRECTORY>'
sudo docker compose -p vonk-forge ps -a
curl --fail --silent --show-error \
  'http://127.0.0.1:8080/api/v1/readyz'
sudo docker compose -p vonk-forge exec -T caddy \
  getent hosts control-api
sudo docker compose -p vonk-forge logs --since 30s caddy
```

All successful one-shots must show exit code 0, readiness must return
`{"status":"ready"}`, and `getent` must return one project-network address.
Wait through at least one subsequent agent claim/observation interval and
repeat the log and fleet checks. Continuing 502s, absent DNS, or stale agents
are blockers; do not start or resume a recipe until they are resolved.

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

This NAS rule does not protect ports published by Docker on a Spark. Docker
diverts published traffic before ordinary UFW `INPUT` rules, so an active UFW
policy alone is not acceptance evidence. Keep Docker's own firewall management
enabled and put site policy in the `DOCKER-USER` chain (or an equivalent
Docker-aware persistent firewall manager). Do not set Docker's `iptables` or
`ip6tables` daemon options to false and do not replace NVIDIA's daemon
configuration.

Before any real workload starts, install and persist these exact host rules on
each possible endpoint owner:

- allow only `<NAS_MANAGEMENT_IP>` to the node's original management
  destination and every configured original published host port, then drop
  every other source to each original destination and port;
- on rank 0, allow only the declared peer fabric address to the original local
  fabric destination and TCP rendezvous port `29500`, then drop every other
  source to that destination and port;
- drop externally forwarded traffic to a non-entrypoint rank's original fabric
  destination and configured original published host port. The local agent
  performs readiness on the host and does not require a remote client
  allowance.

Rules in `DOCKER-USER` see packets after destination NAT. Match the original
published address and port with conntrack (`--ctorigdst` and
`--ctorigdstport`), not the container's changing bridge address. Include an
`ESTABLISHED,RELATED` return before the new-flow rules. These are dedicated
workload nodes: after the explicit allowances, deny every unlisted
Docker-published TCP port whose original destination is either node address,
then return traffic for other destinations. After every Docker restart or host
reboot, verify the dedicated chain is still the first site-policy jump from
`DOCKER-USER` before permitting a workload start.

The signed agent package owns the canonical IPv4 rule shape through
`vonk-forge-docker-firewall.service`. Do not copy ad-hoc `iptables` commands
from an old installation. Create this root-owned site file separately on each
node; the addresses differ per node and `VONK_ENDPOINT_HOST_PORTS` is the
comma-separated list of original published host ports accepted by installed
recipes. The current development recipe publishes host port `8000`. If a later
accepted recipe declares another host port, add it before installation and
reload the service before permitting that recipe to start.

Connected multi-node recipes may instead use the narrowly accepted host-network
shape required by the native Spark runtime. Put their API ports in
`VONK_HOST_ENDPOINT_PORTS`; the current MIA DeepSeek V4 Flash recipe uses
`VONK_HOST_ENDPOINT_PORTS=8888`. The service places `VONK-FORGE-HOST` first in
`INPUT`, permits those
ports only from loopback and the NAS management address, and drops every other
source. It also permits TCP and UDP to the selected fabric address only from
the declared peer on the selected fabric interface, which covers the native
runtime's dynamic peer transport without opening the management interface.
TCP and UDP from loopback are accepted only when both source and destination
are the node's selected fabric address, allowing a distributed rank to join
its own rendezvous store without widening peer or management access.

```ini
VONK_NAS_MANAGEMENT_IP=<NAS_MANAGEMENT_IP>
VONK_NODE_MANAGEMENT_IP=<THIS_NODE_MANAGEMENT_IP>
VONK_NODE_FABRIC_IP=<THIS_NODE_SELECTED_FABRIC_IP>
VONK_PEER_FABRIC_IP=<PEER_SELECTED_FABRIC_IP>
VONK_ENDPOINT_HOST_PORTS=8000
VONK_HOST_ENDPOINT_PORTS=8888
VONK_RENDEZVOUS_PORT=29500
```

Install and activate it only while no Vonk workload is running:

```bash
sudo install -o root -g root -m 0600 /dev/null \
  /etc/vonk-forge-agent/docker-firewall.conf
sudoedit /etc/vonk-forge-agent/docker-firewall.conf
sudo systemctl enable --now vonk-forge-docker-firewall.service
sudo systemctl is-active vonk-forge-docker-firewall.service
sudo /usr/lib/vonk-forge/vonk-forge-docker-firewall \
  --config /etc/vonk-forge-agent/docker-firewall.conf check
sudo iptables -w -S DOCKER-USER
sudo iptables -w -S VONK-FORGE
sudo iptables -w -S VONK-FORGE-HOST
```

The parser accepts only those seven keys, canonical IPv4 addresses and ports,
and a root-owned non-writable regular file. It also requires the node's
management and fabric addresses to be assigned exactly once on two different
local interfaces; allow rules are bound to those derived ingress interfaces.
The service requires the
Spark-managed Docker daemon, refuses a foreign `VONK-FORGE` chain, inserts its
`vonk-forge-managed-v1` jump first, and validates the complete rule count after
each application. The privileged runtime helper requires this service, so a
missing or invalid site policy prevents a Docker workload launch without
blocking offline package installation or pairing. A recipe port omitted from
`VONK_ENDPOINT_HOST_PORTS` remains covered by the node-address default drop: the
run becomes unreachable and fails acceptance, but the unlisted Docker-published
TCP port is never exposed.

Immediately before a host-network workload starts, the privileged helper runs
the packaged `check-host-port` action for the exact `VONK_LISTEN_PORT`. Missing
authorization or firewall drift fails the start before Docker is invoked. Set
`VONK_HOST_ENDPOINT_PORTS=` when no accepted host-network recipe is installed.

Prove lifecycle coupling once during fresh-node acceptance, with no Vonk or
other irreplaceable Docker workload active:

```bash
sudo systemctl restart docker
sudo systemctl is-active vonk-forge-docker-firewall.service
sudo /usr/lib/vonk-forge/vonk-forge-docker-firewall \
  --config /etc/vonk-forge-agent/docker-firewall.conf check
```

The check must pass after the Docker restart and after a host reboot. Stopping
the policy leaves its existing rules in place; Docker restart clears and then
recreates its chains, and the coupled unit reapplies Vonk policy before the
runtime helper can start. Vonk containers use `--restart no`, so they cannot
race policy restoration.

IPv6 workload publications are unsupported by this contract and are rejected
by the privileged helper. Do not infer that the IPv4 chain protects IPv6; add
an equivalent signed `ip6tables` service and acceptance suite before widening
the helper boundary.

## Package installation and pairing

On each GPU node, follow the signed APT steps in
[`docs/operations/agent-package-release.md#install-the-dev-channel`](../operations/agent-package-release.md#install-the-dev-channel),
then install the Rust package:

```bash
sudo apt update
sudo apt install vonk-forge-agent
apt-cache policy vonk-forge-agent
agent_uid="$(id -u vonk-agent)"
test "$(loginctl show-user vonk-agent -p Linger --value)" = yes
test -S "/run/user/${agent_uid}/bus"
```

Do not enable both `dev` and `stable`. Complete the NVIDIA Docker/CDI preflight
from the installation guide. Copy only the public `controller-ca` and public
`host-runtime-grant-public-key` to each node and independently record the CA's
public DER SHA-256 fingerprint:

```bash
openssl x509 -in '<LOCAL_SECRETS_DIR>/controller-ca' -outform DER | sha256sum
scp '<LOCAL_SECRETS_DIR>/controller-ca' '<SPARK_1_SSH_TARGET>:/tmp/controller-ca.pem'
scp '<LOCAL_SECRETS_DIR>/controller-ca' '<SPARK_2_SSH_TARGET>:/tmp/controller-ca.pem'
scp '<LOCAL_SECRETS_DIR>/host-runtime-grant-public-key' \
  '<SPARK_1_SSH_TARGET>:/tmp/host-helper-authority.pub'
scp '<LOCAL_SECRETS_DIR>/host-runtime-grant-public-key' \
  '<SPARK_2_SSH_TARGET>:/tmp/host-helper-authority.pub'
```

On each node, install that certificate as root and set the complete
`/etc/vonk-forge-agent/agent.toml` inputs from
[Install the Vonk Forge agent](../operations/install-vonk-agent.md):
`enrollment_url`, `controller_url`, `ca_path`, the DER `ca_sha256`, that
node's unique `node_id`, `fabric_address`, and `fabric_bandwidth_mbps = 200000`.
Install the helper key at `/etc/vonk-forge-agent/host-helper-authority.pub` as
`root:root` mode `0644`. Use `https://<ENROLLMENT_HOSTNAME>:8443/` for
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
  --signing-key-file '<LOCAL_SECRETS_DIR>/token-signing-key' \
  --ttl-seconds 21600
test "$(stat -c '%a' '<EVIDENCE_DIRECTORY>/admin-token')" = 600
test "$(stat -c '%a' '<LOCAL_SECRETS_DIR>/litellm-master-key')" = 600
```

The helper prints only the token path. First configure the NAS runbook's
[restricted operator loopback forwarding](development-nas-installation.md#restrict-operator-loopback-forwarding).
Open one long-lived SSH tunnel in a separate terminal:

```bash
ssh -N \
  -L <LOCAL_API_PORT>:127.0.0.1:8080 \
  -L <LOCAL_INFERENCE_PORT>:127.0.0.1:4000 \
  '<NAS_SSH_TARGET>'
```

Use `http://127.0.0.1:<LOCAL_API_PORT>` as `--api-base` and
`http://127.0.0.1:<LOCAL_INFERENCE_PORT>` as `--inference-base`. Confirm the
fleet shows both exact node IDs online, Rust protocol 3, fresh inventory,
both `build.rootless-podman.v1` and `runtime.spark-docker-nvidia.v1`, and
distinct management/fabric addresses before
starting a workload. Retain only bounded API output in the evidence directory.

`127.0.0.1:4000` terminates at Caddy's lease-gated internal `:8081` listener;
no LiteLLM port is published to the host. Caddy evaluates the current
route-serving lease at request admission. A request whose Caddy authorization
begins at or after lease expiry is never forwarded to LiteLLM. If the
supervisor authority is unavailable, Caddy fails closed without contacting
LiteLLM. A same-config lease renewal replaces the deadline without restarting
the healthy LiteLLM child.

## Synthetic lifecycle

Run the deterministic native-v1 synthetic fixture through every public API
stage. Its entity graph lives with the fixture; it does not read a prototype
catalog:

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
  --evidence-file '<EVIDENCE_DIRECTORY>/synthetic.json'
```

The final states include source verification, image build/distribution,
install, run, route publication, exact deterministic inference, stop, route
withdrawal, and uninstall.

Recipe image distribution uses bounded 8 MiB range requests from the
controller's content-addressed store. The controller verifies the complete
archive while accepting the builder upload; each target agent independently
rehashes the completed download before requesting Docker import. The range
endpoint therefore streams only the requested bytes and never recopies or
rehashes a multi-gigabyte archive for every range. If an import fails with
`exact OCI image archive is unavailable` and its operation-private staging
file remains zero bytes, inspect the controller/Caddy response for the first
range request; Docker has not been invoked and deleting image or model caches
is not a remedy.

An interrupted unsafe import can end in `waiting-for-operator` rather than
`failed`. Retry that parent image-distribution operation through the same
authenticated recipe retry endpoint. The controller requeues the exact stored
plan, target set, authority digest, and per-node payloads; it does not re-plan
against current inventory. Reusing the same request key replays the same retry,
and a second active retry is rejected. Do not delete the repository, image,
model, or agent-state volumes to clear this state.

## Real single-node model

The native DS4 recipe is
`config/recipes/deepseek-v4-flash-0731-ds4-single.json`. It binds the canonical
DS4 source, runtime distribution, imatrix target GGUF, and DSpark support GGUF
by immutable revision and digest. Model artifacts must already exist beneath
the selected artifact root; container qualification performs no network
fetches.

Fail closed unless the exact generic DS4 runtime above is anonymously public,
the checked source runtime and the wrapper's sole `FROM` match it, and every
fact is current. The wrapper build is networkless and single-stage; it copies
only the local model and rendezvous wrappers because the accepted runtime
already provides `/opt/vonk/busybox`. Then qualify:

```bash
cd '<REPOSITORY_CHECKOUT>'
scripts/qualify-development-model \
  --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --level structural \
  --output '<EVIDENCE_DIRECTORY>/ds4-structural.json'
```

On a supported linux/arm64 Spark with Docker and the installed artifacts, run
the full container path:

```bash
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase model-single \
  --qualification-file '<EVIDENCE_DIRECTORY>/ds4-structural.json' \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/model-single.json' \
  --timeout-seconds 1800 \
  --stop-after inference-ok
```

Restart the NAS stack and agent, wait for fresh inventory, then resume with
the identical command and evidence file without `--stop-after`. The
86,720,111,488-byte target and
5,989,114,272-byte support model remain separate immutable cache objects. An
exact verified cache hit must not download either model again.

## Real multi-node failure and recovery

The native Mia recipe is genuine vLLM multiprocessing tensor parallelism with
world size 2, not a replicated DS4 service. The runtime compiler projects
rank-specific local/master rendezvous addresses and the exact HCA, socket, and
GID environment from the verified distribution capability. Rank 1 runs
headless; only rank 0 owns the routed inference endpoint. The selected input is
`config/recipes/deepseek-v4-flash-0731-mia-dual.json`.

Accepted workloads use the Spark-provided Docker/NVIDIA runtime on an isolated
Docker bridge. Only the endpoint-owning rank publishes its model endpoint on
its management address for the NAS gateway. Every non-entrypoint rank publishes
its health endpoint only on its controller-accepted direct-fabric address, so
it is never exposed on the management LAN. Rank 1 reaches the exact
`VONK_MASTER_ADDR:VONK_MASTER_PORT` through host routing/SNAT; it does not bind
the host's `VONK_LOCAL_ADDR` inside its container namespace.
`VONK_LOCAL_ADDR` remains the bounded controller-supplied rank identity carried
in the HELLO and retained in evidence. The address-specific rank-0 host
publication, host routing, and direct-fabric-only firewall policy enforce the
physical path. Do not treat the peer address observed inside the Docker bridge
as proof of a host fabric source address.

Before starting this phase, verify the Docker-aware GPU-node host firewall for the
current reserved rendezvous TCP port `29500`. The only allowed flow is
`<SPARK_2_FABRIC_IP>` to `<SPARK_1_FABRIC_IP>:29500`. Reject that port from
every other source and on every management or public interface. The rank-0
agent publication must be the address-specific mapping
`<SPARK_1_FABRIC_IP>:29500:29500`, never
`29500:29500`, `0.0.0.0:29500:29500`, or a management-address mapping. A broad
listener or firewall rule is an acceptance blocker, not a temporary fallback.

Retain a redacted copy of `iptables -S DOCKER-USER` and the dedicated site
chain, and perform one
positive probe from rank 1 over its direct-fabric address plus negative probes
from the management path and any public path present at the site. The positive
probe must reach rank 0 only after its coordinator starts; every negative probe
must be refused or time out. Also prove the non-entrypoint model port is absent
from its management address. Use the site's persistent firewall tooling
without disabling Docker's firewall management, adding a wildcard rule, or
exposing the rendezvous port on the NAS.

The positive probe is a protocol exchange, not `nc -z` or another empty TCP
connection. After rank 0 is listening, run this on rank 1 with the accepted
addresses substituted:

```bash
worker_hello='vonk-fabric-v1 worker rank=1 world=2 address=<SPARK_2_FABRIC_IP>'
expected_ack='vonk-fabric-v1 master rank=0 world=2 address=<SPARK_1_FABRIC_IP> port=29500'
actual_ack="$({
  printf '%s\n' "$worker_hello"
} | nc -n -s '<SPARK_2_FABRIC_IP>' -w 5 '<SPARK_1_FABRIC_IP>' 29500)"
test "$actual_ack" = "$expected_ack"
```

An empty or partial client is intentionally rejected after the bounded
`VONK_FABRIC_RENDEZVOUS_SECONDS` read timeout. It is not positive-path
evidence and must not terminate the persistent coordinator.

Run the complete two-rank path through the sole entrypoint:

```bash
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase model-multinode \
  --qualification-file '<EVIDENCE_DIRECTORY>/mia-structural.json' \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_2_NODE_ID>' \
  --failure-node '<SPARK_2_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/model-multinode.json' \
  --timeout-seconds 3600 \
  --stop-after inference-ok
```

Read the non-secret run ID from the acceptance evidence and stop only the
second rank's exact Vonk-managed Docker container. Verify all three management
labels before touching it. Keep the Rust agent running so this proves
workload-rank failure rather than loss of agent presence:

```bash
RUN_ID="$(jq -r '.outputs.run_id' '<EVIDENCE_DIRECTORY>/model-multinode.json)"
test "$RUN_ID" != null
ssh '<SPARK_2_SSH_TARGET>' sudo docker container inspect \
  --format '{{index .Config.Labels "ai.vonkforge.managed"}} {{index .Config.Labels "ai.vonkforge.run-id"}} {{index .Config.Labels "ai.vonkforge.runtime-request-sha256"}}' \
  "vonk-$RUN_ID"
# Continue only when the output is: true, the exact RUN_ID, and 64 lowercase hex.
ssh '<SPARK_2_SSH_TARGET>' sudo docker stop "vonk-$RUN_ID"
```

The authenticated agent submits complete local run snapshots no more than ten
seconds apart while a managed run exists. The distributed lifecycle consumer
must observe the failed rank and withdraw the route before recovery. Recover
the exact stopped rank without rebuilding or deleting its managed state:

```bash
ssh '<SPARK_2_SSH_TARGET>' sudo docker start "vonk-$RUN_ID"
```

This rank-only start applies only to runtimes whose workers can rejoin a live
coordinator. A recipe runbook may instead require a coordinated restart of all
exact managed containers in the gang. Follow that recipe-specific rule while
preserving the same run ID, containers, installation, and caches.

The lifecycle consumer performs bounded worker-first recovery, starts the
endpoint owner last, and republishes only after both ranks have fresh healthy
evidence. Container qualification behaviorally verifies rank-loss route
withdrawal and worker-first recovery. Do not stop an agent, remove a container,
delete a cache/model directory, replace an identity, or delete a named volume
to simulate rank failure outside the controlled qualification path.

For both model phases, canonical qualification evidence is written atomically
with mode `0600`. Keep it private even though its recorded public artifact
identities are non-secret.

Managed model containers retain a read-only root filesystem. The signed host
helper adds exactly one 1 GiB `/tmp` tmpfs with `rw,nosuid,nodev,mode=1777` for
runtime lock and temporary files; do not replace it with a writable root or an
unbounded host mount. While the health endpoint is pending, the agent verifies
every ten seconds under the inspect-only signed helper action that the same
labeled container is still running. That action cannot create or restart a
missing container. An absent or exited container must fail the operation and be
cleaned up rather than waiting on continued lease renewals.

## Restart persistence

The latest official MIA tensor-parallel workload uses the same operation
states with a different immutable recipe and Hugging Face snapshot. Follow the
dedicated [MIA DeepSeek V4 Flash two-Spark runbook](mia-deepseek-v4-flash.md)
for its exact qualification, host-network firewall, build, run, recovery, and
cleanup commands.

For the single-node checkpoint, restart the target agent supervisor, then use
the NAS UI durability action **Stop project**, wait until the project is
stopped, and then **Start project**. Its CLI equivalent, run from the project
directory, is the ordered project stop followed by the full Compose start:

```bash
docker compose stop
docker compose up -d --wait
```

Do not combine `docker compose restart` with a dependency-reconciling
`docker compose up`: the cohort reset can then run after API/worker have
already started. For the multi-node checkpoint, restart both supervisors and
repeat the same NAS project durability action after recovered inference. Keep
all named volumes and do not pull a different cohort during this gate.

Wait until the same two `spk_` identities and fresh inventory return. Rerun the
same native recipe qualification with a new evidence filename. It must use the
same immutable recipe, model, source, and image identities without rebuilding
or redownloading immutable content.

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

Uninstall revalidates the bounded local installation specification and exact
recipe content digest before removing that installation's metadata. It does
not re-hash shared model payloads: those immutable artifacts are verified when
they are acquired and before they are used. Treat an uninstall that scales
with model size as a runtime defect, not as an expected cleanup delay.

## Rollback and secret rotation

Normal development update is an unchanged mutable Compose file followed by
operator **Pull** and **Redeploy**. Keep named volumes. A pinned Compose artifact
is for explicit reproduction or the guarded, schema-compatible recovery in
[Development NAS installation](development-nas-installation.md#advanced-guarded-recovery);
it is not a second mutable channel. Database-incompatible rollback requires a
matching full-state restore.

Rotate the PostgreSQL password and database URL only as one coordinated pair.
Rotate Git signing authority with historical public-key retention. Rotate
agent/controller PKI, host-runtime signing authority, LiteLLM/proxy tokens,
LiteLLM database password, and token-signing authority as one planned new
22-file local
source generation: back it up, distribute replacement public trust first,
schedule re-enrollment/client key change, install the replacement helper public
key on every node before switching the private signer, project the exact
18-file NAS bundle,
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
uv run --project control --frozen scripts/dev-runtime-secrets.py \
  --secrets-dir "$ACCEPTANCE_ROOT/secrets" \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>' \
  --tailscale-oauth-client-id-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-id' \
  --tailscale-oauth-client-secret-file '<LOCAL_OAUTH_INPUT_DIRECTORY>/client-secret'
uv run --project control --frozen scripts/dev-runtime-project \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir "$ACCEPTANCE_ROOT/secrets" \
  --destination "$ACCEPTANCE_ROOT/project" \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --direct-fabric-cidrs '<DIRECT_FABRIC_CIDRS_OR_NONE>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
docker compose -f "$ACCEPTANCE_ROOT/project/docker-compose.yaml" config --quiet
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
for path in \
  /etc/sudoers.d/vonktemp \
  /etc/sudoers.d/99-vonk-codex-temporary
do
  if test -e "$path" || test -L "$path"; then
    echo "temporary sudo path remains: $path" >&2
    exit 1
  fi
done
set +e
sudo_error=$(LC_ALL=C sudo -n true 2>&1)
sudo_status=$?
set -e
if test "$sudo_status" -ne 1 || \
  test "$sudo_error" != 'sudo: a password is required'
then
  echo "unexpected unattended-sudo result: status $sudo_status" >&2
  exit 1
fi
echo PASSWORD_REQUIRED
```

`PASSWORD_REQUIRED` is the success result. A missing `sudo` binary, sudoers
parse or policy error, unexpected exit status, changed diagnostic, regular
file, or symlink at either path fails the gate. Disable NAS SSH in its UI
afterward when that is the site's normal posture. Do not declare the physical
acceptance complete while either temporary sudo path remains.

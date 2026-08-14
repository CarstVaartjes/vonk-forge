# MIA DeepSeek V4 Flash on two DGX Sparks

This is the reproducible development path for one tensor-parallel DeepSeek V4
Flash service across exactly two DGX Sparks. It uses the public recipe from
MiaAI-Lab, but every mutable upstream input is resolved to an immutable identity
before Vonk accepts it.

## Pinned release

| Input | Accepted identity |
|---|---|
| MIA source | `f752cd04ab30f2cf42077dd8811a5e1e682d63e7` |
| Model | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| Model revision | `9e165c30e2704aec5d9d593cce3eebd58bbef1cb` |
| Model bytes per Spark | `166898660330` |
| Runtime | `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8` |
| API port | `8888` |

The source bundle includes MIA's current reviewed hotfixes, including the
GPU-resident per-request thinking budget, safe tool-call truncation, bounded
decode service from issue 43, and suppress-stops-during-reasoning. The
networkless image build applies them once and the runtime root filesystem
remains read-only. Optional scheduler diagnostics stay off by default. Rank 1
runs headless; rank 0 owns the OpenAI-compatible endpoint. Clients may omit
`thinking_token_budget` for the normal fast path, use `0` to skip reasoning, or
set a positive token limit; the server does not inject a hidden default.

No Hugging Face token is needed or accepted. The public model is downloaded as
a separate content-addressed artifact on each Spark. No provider token, NAS
secret, signing key, model weight, or Docker socket is copied into the source
bundle or runtime image.

## Before the first run

Complete the generic
[development agent workload prerequisites](development-agent-workloads.md):
the NAS stack must be healthy, both Rust agents must be paired and current,
their selected 200 Gb/s direct-fabric addresses must be fresh, and the accepted
development agent package must be installed.

Budget at least 471,236,274,132 free bytes and 124,000,000,000 available memory
on each Spark. A cold run downloads 166,898,660,330 model bytes to each node and
pulls about 9.8 GB of compressed runtime layers on the builder. Keep the nodes
on power and wired networking; rerunning the same accepted operation reuses
verified immutable content.

On both Sparks, add the host endpoint to the existing root-owned firewall site
file:

```ini
VONK_HOST_ENDPOINT_PORTS=8888
```

Reload and verify the packaged policy:

```bash
sudo systemctl reload vonk-forge-docker-firewall.service
sudo /usr/lib/vonk-forge/vonk-forge-docker-firewall \
  --config /etc/vonk-forge-agent/docker-firewall.conf check
sudo /usr/lib/vonk-forge/vonk-forge-docker-firewall \
  --config /etc/vonk-forge-agent/docker-firewall.conf check-host-port 8888
```

The `VONK-FORGE-HOST` chain permits TCP 8888 only from loopback and the NAS
management address. Spark peer TCP/UDP is accepted only on the selected fabric
interface, from the declared peer, to the selected local fabric address. It
also permits a process to reach its own selected fabric address over loopback;
PyTorch rendezvous requires rank 0 to join its own store through that address.
The exact peer-fabric acceptance must precede host-endpoint port drops so the
headless worker can reach rank 0's readiness endpoint. Do not add a wildcard
`INPUT` rule.

## Qualify the exact inputs

Create a private mode-`0600`
`<EVIDENCE_DIRECTORY>/mia-qualification-input.json` from current read-only
observations. It uses the same node and runtime-image fields documented in
[Development agent workload acceptance](development-agent-workloads.md#real-single-node-model).
For each node, the artifact evidence must be exactly:

```json
{
  "id": "model",
  "repository": "deepseek-ai/DeepSeek-V4-Flash-0731",
  "revision": "9e165c30e2704aec5d9d593cce3eebd58bbef1cb",
  "bytes": 166898660330
}
```

The top-level `accepted_licenses` must contain `deepseek-model` and `mia-mit`.
Qualify against the MIA sidecars rather than the older DS4
defaults:

```bash
scripts/qualify-development-model \
  --source config/recipes/development/mia-deepseek-v4-flash-source.json \
  --artifacts config/recipes/development/mia-deepseek-v4-flash-artifacts.json \
  --topology config/recipes/development/mia-deepseek-v4-flash-multinode.json \
  --evidence '<EVIDENCE_DIRECTORY>/mia-qualification-input.json' \
  --output '<EVIDENCE_DIRECTORY>/mia-qualification.json'
```

Qualification verifies public ARM64 image identity, the exact source and model
revisions, accepted licenses, NVIDIA/Spark runtime facts, free memory/disk, and
the common direct fabric. The agent later downloads every snapshot file itself,
checks Git LFS SHA-256 metadata where supplied, verifies the exact aggregate
byte count, and writes its own content manifest.

## Build, install, and start

Keep the existing private admin and LiteLLM token files and operator SSH tunnel.
Run the normal acceptance driver with the MIA recipe explicitly selected:

```bash
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase model-multinode \
  --recipe config/recipes/development/mia-deepseek-v4-flash.json \
  --qualification-file '<EVIDENCE_DIRECTORY>/mia-qualification.json' \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_2_NODE_ID>' \
  --failure-node '<SPARK_2_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/mia-multinode.json' \
  --timeout-seconds 21600 \
  --stop-after inference-ok
```

The accepted path builds the image on Spark 1, distributes its exact OCI
identity, installs the model independently on both nodes, starts both ranks,
waits for `/v1/models`, publishes one NAS route, and requires a real chat
response through LiteLLM. The MIA launcher selects only the model revision from
the signed runtime contract and keeps Hugging Face and Transformers offline
while serving. It derives the fabric interface directly from
`/sys/class/infiniband` by matching the controller-selected IPv4 address to one
unique RoCEv2 GID, so it does not depend on `iproute2` inside the runtime image.
If that lookup fails, verify the selected direct-fabric address and the node's
RoCEv2 GID configuration; do not add packages to a running container.

Rank 1 remains a native vLLM headless worker, so it does not provide its own
OpenAI API. A minimal runtime-local readiness proxy exposes only
`/v1/models` on rank 1 and forwards that probe over the selected fabric to rank
0. The proxy runs only while the headless vLLM process is alive. Rank 0 cannot
serve that endpoint until both tensor-parallel ranks have joined, so each
agent's normal local readiness check proves the worker process is alive and the
complete two-rank engine is serving. Client traffic and the published route
still terminate only at rank 0.

After a long image import, authenticated inventory can briefly be older than
the installation admission limit. The driver retries only previews whose sole
blocker is `install.stale_inventory`, for at most two minutes and within the
overall timeout. Every other blocker remains an immediate failure, and no
installation is submitted until a fresh preview is allowed.

## Connect Pi

Create a dedicated LiteLLM virtual key restricted to the stable `deepseek`
alias. Never copy `litellm-master-key` to a workstation. From the trusted local
inference tunnel, with shell tracing disabled:

```bash
set +x
umask 077
master_key="$(< '<LOCAL_SECRETS_DIR>/litellm-master-key')"
curl -fsS 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>/key/generate' \
  -H "Authorization: Bearer $master_key" \
  -H 'Content-Type: application/json' \
  --data '{"models":["deepseek"],"key_alias":"pi-dev"}' \
  | jq -er '.key' > '<LOCAL_SECRETS_DIR>/pi-litellm-key'
unset master_key
```

Store that generated value in the operator's password manager as
`Vonk Forge Pi/LiteLLM API Key`. On the Tailscale-connected workstation, save
this provider in `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "vonk-forge": {
      "baseUrl": "https://vonk-forge.tail46101a.ts.net/v1",
      "api": "openai-completions",
      "apiKey": "$VONK_PI_API_KEY",
      "authHeader": true,
      "compat": {
        "supportsStore": false,
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": true,
        "supportsUsageInStreaming": true,
        "maxTokensField": "max_tokens"
      },
      "models": [
        {
          "id": "deepseek",
          "name": "Vonk Forge DeepSeek V4 Flash",
          "reasoning": true,
          "input": ["text"],
          "contextWindow": 1048576,
          "maxTokens": 32768,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          }
        }
      ]
    }
  }
}
```

Load the scoped key without writing it into Pi's configuration, then select the
provider and model:

```powershell
$env:VONK_PI_API_KEY = op read 'op://Private/Vonk Forge Pi/LiteLLM API Key/password'
pi --provider vonk-forge --model deepseek
```

The workstation must be connected to the same tailnet. Revoke this virtual key
in LiteLLM if the workstation or password-manager item is compromised.

## Failure, recovery, and cleanup

Use the run ID in the private evidence file. Follow the generic
[rank failure and recovery procedure](development-agent-workloads.md#real-multi-node-failure-and-recovery):
inspect all Vonk management labels, stop only rank 1's exact managed container,
resume to `--stop-after route-withdrawn-after-failure`, start that same
container, and resume to `--stop-after inference-recovered`.

Then perform the documented supervisor and NAS restart checkpoint and run the
same command without `--stop-after`. It must prove fresh agents, healthy ranks,
route persistence, and inference before it stops the run, withdraws the route,
and uninstalls normally.

An interrupted attempt is resumed with the identical command and evidence path.
Do not delete model/image caches, agent state, or containers to make a retry
pass. Rollback means stop and uninstall the accepted recipe revision; it never
means changing a mutable tag or editing a running container.

If artifact acquisition ends in a failed installation, rerun that identical
command. The acceptance driver submits one exact retry of the stored install
plan, retaining the same installation identity, node set, authority digest,
and immutable model and image caches. A second terminal failure stops the
driver for diagnosis; it does not trigger another retry or destructive cache
cleanup.

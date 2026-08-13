# MIA DeepSeek V4 Flash on two DGX Sparks

This is the reproducible development path for one tensor-parallel DeepSeek V4
Flash service across exactly two DGX Sparks. It uses the public recipe from
MiaAI-Lab, but every mutable upstream input is resolved to an immutable identity
before Vonk accepts it.

## Pinned release

| Input | Accepted identity |
|---|---|
| MIA source | `3c9576c52ab71d89e22fe4621e0d32300a59039a` |
| Model | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| Model revision | `9e165c30e2704aec5d9d593cce3eebd58bbef1cb` |
| Model bytes per Spark | `166898660330` |
| Runtime | `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8` |
| API port | `8888` |

The source bundle includes MIA's current reviewed hotfixes, including its
suppress-stops-during-reasoning fix. The networkless image build applies them
once and the runtime root filesystem remains read-only. Rank 1 runs headless;
rank 0 owns the OpenAI-compatible endpoint.

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
interface, from the declared peer, to the selected local fabric address. Do not
add a wildcard `INPUT` rule.

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

The top-level `accepted_licenses` must contain `deepseek-model` and
`mia-apache-2.0`. Qualify against the MIA sidecars rather than the older DS4
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
while serving.

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

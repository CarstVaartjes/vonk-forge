# Runtime release deployment

This is an archived SSH-controller compatibility tool, retained only for
explicit migration and recovery of the retired `vonkctl-legacy` path. It is
not part of a fresh install or the outbound-agent recipe runtime. Do not use it
to deploy a new Spark workload.

Use `scripts/deploy-runtime-release` from the developer machine to install the
selected workload's checked adapter release on exactly its resolved GPU node
nodes. For V2 repositories, targets come from `inventory/fleet.toml` and the
content-addressed `inventory/placements/<workload>.json` plan. Legacy workload
`nodes` and `inventory/cluster.toml` aliases remain a compatibility read path.
This operation deploys only the small, immutable runtime adapter. It
does not download model artifacts, pull container images, start containers, or
change the active Cluster Profile.

## Preconditions

- The workload has a complete `[runtime_release]` block.
- Its manifest is a repository-relative regular file.
- Every manifest entry is a regular file below the manifest's parent directory
  and matches its recorded SHA-256.
- Manifest-listed payloads below `bin/` are installed with mode `0755`; all
  other manifest-listed payloads are installed with mode `0644`. These intended
  release modes come from repository/manifest policy, not the checkout's local
  mode bits, which may appear as `0777`/`0666` on `/mnt/c`.
- V2 placement nodes are unique canonical `spk_…` IDs, exist in the fleet, are
  `ready`, and the plan pins both the workload-file SHA-256 and placement input
  digest. The fleet supplies user, host, and port without embedding credentials.
- On the legacy path, `inventory/cluster.toml` contains hardened SSH aliases for
  every node declared by the workload.
- Host keys have already been accepted through the SSH bootstrap runbook.
- `/opt/node/model-adapters` exists on every target node and is writable by
  the controller SSH user. Bootstrap each target once with:

  ```bash
  sudo install -d -o root -g root -m 0755 /opt/node
  sudo install -d -o carst -g carst -m 0755 /opt/node/model-adapters
  ```

Current examples are `adapters/deepseek/mia-vllm/runtime-manifest.json` for the
two-node Mia workload and `adapters/deepseek/ds4/runtime-manifest.json` for the
GPU node-1-only DS4 workload. Repository paths such as
`adapters/deepseek/mia-vllm/bin/mia-deepseek-dual` are installed with their
manifest-parent prefix removed.

## Review the dry run

Dry run is the default and executes no SSH or transfer command:

```bash
scripts/deploy-runtime-release deepseek-agent-dual
```

The JSON plan identifies the exact manifest digest, canonical node IDs,
resolved management targets, stripped release paths, and immutable destination:

```text
/opt/node/model-adapters/deepseek-agent-dual/releases/<manifest-sha256>/
```

Resolve every local manifest or payload error before applying. Do not bypass a
digest mismatch by editing only the workload pin; regenerate and review the
release manifest after all release files are final.

## Apply to the workload-declared nodes

Writing requires the explicit flag:

```bash
scripts/deploy-runtime-release --apply deepseek-agent-dual
```

For each resolved node, the deployment performs these gates in order:

1. Probe the digest-qualified final directory. An exact existing tree is an
   idempotent success; any differing file, directory, symlink, or hash is
   refused.
2. Create a unique temporary directory whose name contains the full manifest
   digest below the final `releases/` directory.
3. Create only manifest-implied subdirectories and transfer only manifest-listed
   payload files.
4. On the node, require the exact file and directory counts, reject symlinks or
   special files, and recompute every payload SHA-256 and expected mode.
5. Atomically rename the verified temporary tree to the immutable final path.

SSH uses batch mode, disables forwarding, requires the configured identity and
strict host-key checking, and never evaluates repository-provided shell text.
The remote scripts receive only locally validated paths and digests as quoted
arguments.

The controller selects native `ssh`/`scp` by default and auto-selects
`ssh.exe`/`scp.exe` on WSL when they are discoverable. Custom commands use
`VONK_SSH_BIN` and `VONK_SCP_BIN`. A custom SCP wrapper must also set
`VONK_SCP_PATH_STYLE=posix` or `VONK_SCP_PATH_STYLE=windows` when its input
path syntax differs from the default POSIX wrapper contract; only those two
exact values are accepted. Auto-selected WSL `scp.exe` uses Windows paths and
native SCP uses POSIX paths regardless of executable basename. After every
copy, the remote mode gate applies repository policy and the final verifier
rechecks the exact mode and SHA-256 before installation.

## Failure handling

A differing final directory is never replaced. Preserve it for diagnosis and
compare it with the checked manifest. A failure before the final rename leaves
the final path untouched; the digest-qualified temporary directory may remain
for inspection. No automatic recursive cleanup runs.

Deployment is atomic per node, not across both nodes. If GPU node 1 installs and
GPU node 2 fails, correct the failure and rerun the same command. GPU node 1 will be
recognized as identical and skipped, while GPU node 2 resumes through a new safe
temporary directory. Do not create a mutable `current` symlink.

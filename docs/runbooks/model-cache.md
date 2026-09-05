# Model cache preparation and verification

This runbook prepares the immutable DeepSeek-V4-Flash-0731 snapshot used by
the Mia dual-GPU node runtime. Each GPU node keeps its own complete copy under
`/srv/models`; the NAS and Hugging Face are not serving-time dependencies.

The Controller-side cache downloader supports optional gated/private access
through the [Hugging Face model-cache authentication guide](../model-cache-huggingface-auth.md).
The default deployment has no `HF_TOKEN_FILE`, so public downloads remain
anonymous. The signed NAS installer creates an empty regular
`secrets/hf-token` with owner-only permissions without prompting. To enable
gated access, replace it with a protected token file (`chmod 0400
secrets/hf-token`), set `HF_TOKEN_FILE=./secrets/hf-token` in the host `.env`,
then recreate or restart the `control-api` and `control-worker` services so the
normalized secret volume is refreshed:

```bash
docker compose up -d --force-recreate control-api control-worker
```

To rotate the credential, replace the file atomically with another owner-only
file and run the same recreate command. The token is used only for a canonical
Hugging Face gated response; the Controller verifies the resulting bytes once
in the NAS cache, and Spark distribution remains tokenless. Missing access is
reported as `model_cache.credentials_missing`; a rejected token is reported as
`model_cache.credentials_denied`. Neither error contains the credential.

## Immutable inputs

| Input | Pinned value |
|---|---|
| Repository | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| Revision | `9e165c30e2704aec5d9d593cce3eebd58bbef1cb` |
| Expected manifest | `manifests/deepseek-v4-flash-0731.json` |
| Manifest SHA-256 | `82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121` |
| Snapshot path | `/srv/models/snapshots/deepseek-v4-flash-0731` |
| Node manifest path | `/srv/models/manifests/deepseek-v4-flash-0731.json` |

The manifest covers 74 files and 166,898,660,330 bytes. Its 48 SafeTensors
shards account for 166,886,535,336 bytes. The required
`encoding/encoding_dsv4.py` is included explicitly.

## Build the expected manifest

Build expected manifests on the developer machine before preparing either
node. Generation calls the exact revision API with `?blobs=true`, requires the
response's top-level `sha` to equal the requested revision, and parses the
pinned weight index. It takes weight SHA-256 values and sizes from Git LFS
metadata. It downloads and hashes only the 26 non-LFS repository files; it
does not download any weight blob. A repository `blobId` is retained only as
Git provenance and is never treated as a raw-file SHA-256.

```bash
uv run python -m tools.model_manifest generate \
  --repo deepseek-ai/DeepSeek-V4-Flash-0731 \
  --revision 9e165c30e2704aec5d9d593cce3eebd58bbef1cb \
  --output manifests/deepseek-v4-flash-0731.json

shasum -a 256 manifests/deepseek-v4-flash-0731.json
```

Review the revision, file count, aggregate sizes, encoder entry, index entry,
and all 48 shard entries before pinning the manifest digest in a Model
Definition. Regeneration is a maintenance action: a changed byte means the
checked manifest and its consumer pins must be reviewed together.

## Prepare each node

Preparation must use the exact commit above and a temporary directory on the
same local filesystem as the final snapshot. The node-local preparation job
downloads into that temporary directory, verifies it, and only then installs
it at the final path. Never resolve `main`, `latest`, or another branch name.

The installed tree must contain materialized regular files. Do not point the
snapshot path at a Hugging Face cache snapshot made of symlinks. If a download
tool uses a shared blob cache internally, its final `--local-dir` output still
has to be a self-contained regular-file tree before verification. The
verifier refuses symlinks, non-regular files, unsafe relative paths,
unmanifested files or directories, missing files, size changes, and digest
changes. It does not repair, download, or delete anything.

Keep the checked manifest outside the snapshot and copy the identical bytes
to the node manifest path. Confirm its digest before using it:

```bash
shasum -a 256 /srv/models/manifests/deepseek-v4-flash-0731.json
```

Expected output begins with:

```text
82e965c1caa019b31f4d776d0b3eddb0cc0d8e076f189822b8a3bbe3fa115121
```

## Verify offline

Run the standard-library-only verifier on each GPU node after download, after any
transfer, and before runtime activation:

```bash
python3 tools/model_manifest.py verify \
  --manifest /srv/models/manifests/deepseek-v4-flash-0731.json \
  --snapshot /srv/models/snapshots/deepseek-v4-flash-0731
```

Verification performs no network calls. It opens each path relative to a
no-follow directory descriptor where the operating system supports it,
requires a regular file, checks size before hashing, and hashes in 8 MiB
chunks. A successful report has `"ok": true`, 74 verified files, no missing,
changed, unsafe, or unexpected paths, and 166,898,660,330 verified bytes. A
failure exits nonzero and prints all discovered snapshot failures as JSON.

After both nodes pass, serving uses:

```text
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
HF_HUB_DISABLE_XET=1
```

Do not delete a previous snapshot, runtime cache, output, or verification
evidence as part of failure recovery. Retain the failed report and staging
directory until the mismatch has been diagnosed.

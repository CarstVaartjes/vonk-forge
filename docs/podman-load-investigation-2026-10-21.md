# Podman base-image import failure, 2026-10-20

This is a source investigation using the owner's retained loginfo files, not a
production change or proof of deployed recovery. Production was not mutated;
no host login or credential-store access was used. Base: `486f7ff1e` (#1839).
The owner reported production tree `d256e985` and rolling agent upgrades;
repository history alone does not establish each node's installed revision.

## Verified observations and source behavior

Both supplied loginfo files retain stderr. Spark 3542 at 22:40–22:46Z and
Spark 2297 at 22:40–22:54Z report `database run root "" does not match our run
root "/run/vonk-forge-agent/b-…": database configuration mismatch`. Each
attempt names a different run-root. The summary and diagnostic retain only
the unclassified import label; the typed subprocess exit code was absent.
Thus this is incomplete evidence projection, not complete loss of stderr.

`rust/crates/vonk-agent/src/recipe_builder/builder.rs` retained operation-owned
`podman-storage` but allocated and destroyed a random run-root per invocation.
The retained-image probe ignored errors, and the next load reused the same
incompatible database. Durable staging dates to `fe3d77eb5` (October 9), before
all the October 20 deployments named in the brief.

Podman 4.9.3's [SQLite validation implementation](https://github.com/containers/podman/blob/v4.9.3/libpod/sqlite_state.go#L382-L406)
resolves stored paths, ignores ENOENT, then compares the resolved strings. A
removed stored run-root becomes an empty string in that diagnostic. This
matches the observed empty stored path and fresh nonempty run-root. An earlier
unsuccessful/interrupted build suffices: its TempDir disappears while its graph
remains. Manual cleanup is not required to reproduce this behavior. This does
not prove the deployed Podman patch version or database contents.

The current base-image store already treats an absent or structurally damaged
managed archive as a miss: it isolates damaged entries, resumes/re-fetches the
exact pinned source, verifies ingress, and publishes atomically. Warm reuse
avoids full layer rehashing. Existing behavior tests exercise missing entries,
truncation, changed-size layers, symlinks, and interrupted transfer recovery.
The logs name database initialization, not archive parsing; no evidence here
justifies changing the archive trust or distribution contract. This path uses
the accepted base-image source through its existing registry ingress; it is
not a new NAS distribution endpoint.

## Timeline: supplied times, not a reconstructed deployment history

| UTC October 20 | Evidence | Implication |
| --- | --- | --- |
| 13:23 | Owner's first repeated import failure on 3542 | Before #1814, #1838 and #1835 |
| 15:17 | Owner's #1814 deployment | Cannot initiate the earlier 3542 failure; re-observation can expose retained state |
| 17:25 | Owner's #1838 deployment | Before 2297's first repeat, but no proof of causation or per-node activation |
| 17:53 | Owner's first repeated import failure on 2297 | Compatible with a later retry/restart reaching the same latent defect |
| 19:16 | Owner's #1835 deployment (#1832/#1833 retention included) | After both onsets; not their initiating cause |
| time unknown | Owner's manual cache cleanup | May force cold builds or remove runtime paths; exact paths/times are unavailable |

#1810/#1812 fix numeric passwd identity inside launched containers. They do not
fix Podman's host-side database path binding. Historical namespace/permission
preflight findings since October 18 are real separate observations; they do
not explain the specific later database-mismatch stderr. Current store
retention distinguishes build claims from reusable base images, marks base
use and coordinates its lock. Nothing in these logs establishes that retention
deleted an in-use archive or graphroot. Cleanup/restarts are plausible triggers
for exposing the pre-existing path defect, not verified causes of the initial
failure times. Neither deployment correlation nor the old preflight findings
establishes a current user-namespace, passwd, permission, or archive-integrity
fault.

## Source repair

Use a short, stable run-root encoding the complete operation UUID. Pin Podman's
`--tmpdir` beneath it so direct and user-service commands share metadata paths.
After exact build services are confirmed settled, a database configuration
conflict discards only that build's private execution cache and returns through
the normal build path once. Verified base archives remain reusable; permissions
and integrity checks are unchanged. There is no Podman-wide reset.

Retain actual subprocess status, including explicit absence of a numeric status,
with bounded sanitized stderr/stdout. Preserve helper command failures across the
framed response, agent normalization, Controller storage and evidence download.
Classification supplements the retained output. No database model/schema change
is required; these are canonical protocol diagnostics.

## Owner-only read-only host commands

Run from an already authorized console on each Spark; these commands were not
executed against production by this track. Read only the listed public config
files, never registry-auth, environment secrets or credential files.

```bash
podman --version
dpkg-query -W vonk-forge-agent podman uidmap fuse-overlayfs
systemctl show vonk-forge-agent.service -p User -p Group -p RuntimeDirectory -p ProtectKernelTunables -p ProtectKernelLogs -p NoNewPrivileges -p RestrictNamespaces
cat /etc/vonk-forge-agent/containers-storage.conf
getent passwd vonk-agent
cat /etc/subuid /etc/subgid
stat -c '%n %U:%G %a %y' /run/vonk-forge-agent /var/lib/vonk-forge-agent/build-staging /var/lib/vonk-forge-agent/base-images
find /var/lib/vonk-forge-agent/build-staging -type f -name db.sql -print
journalctl -u vonk-forge-agent.service --since '2026-10-20 12:30:00 UTC' --until '2026-10-20 23:00:00 UTC' --no-pager --grep='database configuration mismatch|base image|permission denied|userns|cleanup'
journalctl -k --since '2026-10-20 12:30:00 UTC' --until '2026-10-20 23:00:00 UTC' --no-pager --grep='apparmor|DENIED|userns'
```

For each `db.sql` returned, substitute its exact path below. SQLite opens it
read-only; do not run Podman repair, reset, prune, load or migrations for this
investigation.

```bash
sqlite3 -readonly '/exact/path/to/db.sql' 'SELECT StaticDir,TmpDir,GraphRoot,RunRoot,GraphDriver,VolumeDir FROM DBConfig;'
```

The recorded RunRoot and its existence, installed package version/time, plus
the owner's exact manual-cleanup paths/times resolve the remaining onset
attribution. Deployment and physical Spark recovery remain unverified until
the coordinator publishes/deploys the integrated fix and observes a normal
build succeeding without manual cleanup.

## Local verification

The retry fault injection fails with the original `origin/main` builder's
runtime path allocation and succeeds with stable paths and conflict recovery.
Linux/OrbStack checks passed: 34 builder tests, two cleanup tests, six executor
build tests, 200 helper-library tests (one ignored), 61 protocol tests, 11 helper
binary tests, and the helper-to-agent redaction/status intake regression.
The repository guards passed 557 tests with `-n 0`; Controller evidence,
outcome and ingress checks passed 161. Cargo fmt/Clippy (`-D warnings`), Ruff
0.16.9 format/check, Python types, frozen uv sync, and the added-line guard passed.

A broader agent-library run was not green: 492 passed, one failed, two ignored.
The unchanged `client::transport::tests::rotation_silent_activation_retries_same_generation_and_repairs_heartbeat`
failed its `old request overlapped activation` fixture assertion, including
with one test thread. This remains a validation gap; no CI was rerun or cancelled.
Source tests do not establish deployment or physical Spark recovery.

## Follow-up: clean-host acceptance regression

The folded `ce8b9e9d` introduced a separate fresh-host fault. Replaying the
`486f7ff1` and `ce8b9e9d` Podman arguments with Podman 4.9.3 on the local
Ubuntu noble ARM64 OrbStack VM reproduced it without a network image pull:
initialize private storage in the agent environment, then build a scratch image
with a static `RUN` command through a disposable systemd user service whose
`XDG_RUNTIME_DIR` is the operation runroot. The main arguments build successfully;
the stable-path arguments exit 1 before the first build step:

```text
error creating temporary file: No such file or directory
invalid internal status ... open <runroot>/libpod/tmp/pause.pid: no such file or directory
```

An explicit `--tmpdir=<runroot>/libpod` bypasses initialization of Podman's
default rootless pause directory. Direct import starts with the agent runtime;
the build service switches to the operation runtime, whose `libpod/tmp` was
never created. This is not the 50-byte runroot limit, a missing systemd
RuntimeDirectory, or an ownership mismatch. Preparing `libpod/tmp` before the
first Podman command makes the identical real build succeed. The operation
runroot is private (0700); stable identities and the existing one-pass private
database conflict recovery remain in place.

The real-Podman regression in `recipe_builder::builder::runtime_tests` was
observed failing before the initialization change and passing after it. Run it
in the Linux rootless-Podman lane with:

```bash
cargo test -p vonk-agent --lib fresh_rootless_podman_can_switch -- --ignored
```

The ordinary database-retry fixture now also exercises opening the pause file,
so skipping fresh-runtime initialization fails the normal builder suite.

The missing acceptance detail had a second owner: `_observe_build` selected
stderr for `log_excerpt` but constructed `detail` from only category and summary.
It now includes the captured numeric exit (when present) and the sanitized final
output within the preparation detail's bound, and retains the final 1024
characters in `log_excerpt`. The producer/store/projection regression failed
without that change. It checks exit 125, the final cause after long output,
secret redaction, and fresh-request recovery.

All operations in this follow-up were local. No production login, credential
store access, deployment, PR mutation, or CI action was performed. The complete
signed candidate lifecycle still requires its candidate/baseline manifests and
artifact object root; the local Podman boundary is not full release acceptance.

Follow-up gates passed: frozen Controller sync; Ruff 0.16.9 format/lint; changed
Python types; added-line scan against `origin/main`; 557 repository guards with
`-n 0`; Linux agent fmt, Clippy (`--all-targets -D warnings`) and all-target tests;
the explicit real-Podman regression; 176 Linux Controller tests including real
PostgreSQL; and 85 acceptance-harness cases across macOS and the Linux VM.
The earlier transport-test failure also passed in this full Linux agent run.
The scoped PostgreSQL fixture container was removed by its session teardown.

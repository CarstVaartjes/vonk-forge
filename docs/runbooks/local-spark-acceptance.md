# Local Spark installer acceptance

Run the complete `tests/acceptance/test_spark_lifecycle.py run` lane in a fresh,
disposable Ubuntu ARM64 systemd machine with the signed candidate, baseline,
object root, exact Compose overlay, and environment required by
`.github/workflows/installer-publication.yml`. Use the intended OrbStack
Docker engine for container checks. A container-only helper test does not prove
the installed service's systemd isolation or the complete installer lifecycle.

Use a normal disposable OrbStack machine for the complete lane. OrbStack's
isolated-machine mode can block nested Docker image extraction with
`operation not permitted`, before the installer is exercised. Verify the
machine configuration and a real candidate image pull first; do not weaken
the packaged Vonk services to work around a test-machine restriction.

Before installing the package in an OrbStack Linux machine, run from the exact
platform checkout inside that machine:

```sh
sudo python3 tests/acceptance/systemd_sandbox.py prepare-local --disposable-systemd
```

OrbStack generates a global `service.d/zzz-lxc-service.conf` that disables
`ProtectSystem` and other isolation settings and clears writable/read-only path
lists. The preparation command places same-name `/dev/null` masks only under
the three Vonk service drop-in directories. The installed package retains its
own current policy; no global generator or unrelated service is changed.
Deleting the generated global file is insufficient because daemon reload
recreates it. The masks persist across package installation and daemon reload.

The lifecycle harness checks effective filesystem and process-isolation
properties against each installed service fragment immediately after package
installation. A masked policy fails acceptance before the serving canary.
To inspect an already installed disposable environment, run:

```sh
python3 tests/acceptance/systemd_sandbox.py verify
```

If a local policy mismatch is discovered after installation, discard the
disposable acceptance machine and prepare a fresh one before rerunning. Do not
claim that a run performed with relaxed service isolation proves the installed
package. Keep publication acceptance and physical NVIDIA/Spark qualification
as separate evidence boundaries.

On failure, the harness reports the concise run-switch failure, recent phase
identities, Controller log tail, and separately bounded agent and helper
journals. Successful artifact receipt history cannot consume the entire failure
message. Known acceptance credentials are redacted before excerpts are bounded.

## GB10 host memory safety

The privileged Spark agent helper samples MemAvailable, MemFree, swap, memory PSI
(full avg10/avg60) and the swap-in, swap-out and refault counters every 250 ms,
independently of workload commands, and follows only new kernel journal records.
Swap is a load-time cushion: a load that swaps steadily is never stopped. The
swap-aware policy (owner approved 2026-10-28) stops managed workloads when:
R1 MemAvailable < 1 GiB and free swap < max(2 GiB, 10% of swap) for 2 s (no
swap: MemAvailable < 256 MiB for 3 s); R2 swap > 90% used, still swapping out,
MemAvailable < 4 GiB for 3 s; R3 swap-ins plus refaults ≥ 50k pages/s with full
avg10 > 60% and MemAvailable + free swap < 8 GiB for 10 s; R4 full avg10 > 60%
for 30 s while serving, or full avg60 > 80% and not falling for 60 s while a load
runs (the agent's page-cache drop cadence marks a loading Spark). An NVIDIA
`NVRM: Out of memory [NV_ERR_NO_MEMORY]` line (R5, non-fatal alone on GB10)
stops only together with R1's or R3's condition or MemFree < 1 GiB while swap-out
is about zero (always, on a host without swap), held for 2 s like R1 (never one
sample); a load whose host pages are swapping out is absorbed, not killed. There is no per-sample MemAvailable bar.
Every stop's evidence carries, within the eight failure properties the agent
keeps: the trigger, MemAvailable and full avg10, `pressure_readings` (MemFree,
swap, full avg60, swap and refault rates), `sustained_ms`, `recent_samples`
(the last six samples, one per second) and `observed_at`. The thresholds live in
`host_memory_guard_policy.rs`, marked calibratable; the evidence is the
2026-10-07 spark-3542 freeze, the 2026-10-08 GB10 OOM research and the
systemd-oomd, oomd and earlyoom defaults.

The helper verifies the exact agent-managed container identity, SIGKILLs its
own process group when that group can be proved to belong to the container,
then sends container KILL and stop with bounded command deadlines. Unconfirmed
stops are reconciled and retried; no cancellation fence or admission ban is
created. Kit arguments, including `gpu-memory-utilization`, stay unchanged.
This is precursor protection; a driver already wedged cannot be rescued by a
software guard.

Before launch, the agent reports MemAvailable against declared peak plus a
1 GiB informational margin. It never refuses a launch on that estimate.
Safety stops carry `workload.host_memory_exhausted`, exit cause
`host_memory_exhausted`, pressure/available-memory measurements, and observation
time through exit diagnostics and background observations. The Controller
retains these diagnostics and exposes them through durable Fleet events.
Local records under `/var/lib/vonk-forge/host-memory-evidence/` are keyed by
immutable container ID so a new attempt cannot inherit an old failure.
Kernel-follow failures are logged and retried every two seconds; the `/proc`
checks continue independently. Physical qualification must verify that the
packaged helper can follow the kernel journal and stop a managed canary.

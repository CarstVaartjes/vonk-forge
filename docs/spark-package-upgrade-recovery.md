# Spark package upgrade recovery

A Controller-managed Spark upgrade has one recovery path. The package ships no
`preinst` and no recovery runner: nothing in the package completes an
interrupted upgrade forward. The path is

custody -> rollback arm -> dpkg -> finisher -> acknowledgement or deadline
restore -> Controller re-drive.

## 1. Custody

The agent downloads the complete signed candidate and the captured installed
source DEB into its private incoming directory, but the already-installed
signed package helper never passes that agent-owned path to `dpkg`. It opens the
file without following links, validates the artifact (digest and Ed25519
signature, once, at this ingress), copies those exact bytes into a fresh
root-only `/run/vonk-forge-package-candidates/<invocation>/` directory, and
invokes `dpkg` with that custodied path. No later step re-hashes the package.

## 2. Rollback arm

Before `dpkg` can stop the healthy agent, the helper arms the bounded source
restoration, and refuses to install if it cannot. The signed install operation
carries one nested `PackageRollbackAuthority`: source package
digest/signature/version and agent/helper hashes, a fresh attempt nonce, and an
activation deadline (900 s). The helper verifies the source DEB signature,
retains the source bytes and a copy of the source helper executable in a
root-only durable transaction directory, and records the transaction as armed.
The fixed `vonk-forge-package-rollback.service` watchdog runs that retained
executable, so replacing or stopping the installed helper cannot remove the
recovery runner. The unit is enabled by the package, so a reboot resumes an
armed transaction.

The Controller path stays anti-rollback: the helper arms rollback only for a
candidate whose version is at least the exact captured source. A root
operator's explicit `dpkg --install` of an older package is the latest request
and proceeds; maintainer scripts do not refuse a downgrade.

## 3. dpkg

The old helper runs `dpkg --install --force-confold` on the custodied
candidate inside its own unit sandbox. The candidate has no `preinst`, so
nothing arms before unpack. The old package's `prerm upgrade` and `postrm
upgrade` do nothing. The candidate's `postinst configure` converges the
account, directories, machine evidence and unit enablement, retires any
forward-capsule files an older package staged (the old capsule unit, its
drop-ins, its wants link; they are not dpkg-owned), reloads systemd, and
schedules the finisher. A `postinst` failure leaves the package `iF`, the
install fails, and the helper moves the transaction to `activation_failed`, so
the watchdog restores at once.

## 4. Finisher

On every upgrade `postinst` starts the transient
`vonk-forge-package-helper-upgrade-finish.service`, run by PID 1 so it outlives
the helper it restarts. It waits for the dpkg process to exit, stops the agent
and monitor, restarts the helper, proves from `/proc/<pid>/exe` that the
running helper is the candidate's, and then restarts the agent and monitor.
No agent talks to a helper of another release: the old agent stops before the
helper restarts, and the new one starts after the helper is proven. Whatever
happens in between, the finisher's exit starts the agent again; it never stays
stopped. Docker containers belong to dockerd and are untouched.

`postinst` reads the installed helper's digest once and hands it to the
finisher; the `/proc/<pid>/exe` comparison is the only proof of the running
helper (the agent's own identity gate covers the agent). Nothing else is
hashed or re-validated here: there is no pending fence, no receipt, no
`vonk-agent --version` check and no unit-text check. A `helper-upgrade.pending`
or `helper-upgrade.receipt` an older package left is never read; the helper's
periodic store-retention pass removes it.

## 5. Acknowledgement or deadline restore

The new agent contacts the Controller, which issues the signed fixed
`confirm-package-activation` operation only for the candidate that has met the
contact and readiness gate. It binds the candidate package digest and the
original attempt nonce; the host grant binds the node. The helper acknowledges
only when `dpkg` reports the package `install ok installed` at the candidate
version and the installed agent and helper hashes are the candidate's. Any
other or unreadable state refuses, the transaction stays armed, and the
watchdog restores at the deadline: a candidate that is only unpacked or
half-configured is never acknowledged. The acknowledged root receipt is the
final activation gate.

Any other end is restored by the watchdog: a failed install restores
immediately; no acknowledgement restores at the deadline, including after a
crash of the helper, of `dpkg`, or a reboot (the deadline has usually passed
and the enabled watchdog restores at boot). Restoration stops the agent and
helper, reinstalls only the captured source DEB, and verifies the configured
package state plus the actual restarted process executable. Interrupted
restoration resumes from the same durable transaction; it cannot select a
different package or overwrite a third identity. The restore runs `dpkg`
offline, so the source `postinst` schedules no finisher; the watchdog itself
reloads systemd and restarts the helper socket and the agent.

An interrupted upgrade therefore ends on the previous version within the
deadline, not forward on the candidate.

`/var/lib/vonk-forge/package-activation.receipt.json` is a root-owned readable
current schema-2 `PackageActivationReceipt`. It carries the node, source and
candidate versions/package and agent hashes, attempt nonce, phase, creation and
update times, and bounded outcome. It contains no arbitrary shell output,
credentials, package paths, or signature private material. The Controller can
report `armed`, `activation_failed`, `acknowledged`, `rolling_back`,
`rolled_back`, or `rollback_failed` without confusing candidate contact with
activation success.

## 6. Controller re-drive

The Controller retries a failed or unacknowledged order automatically, after
`AGENT_UPGRADE_RECOVERY_FENCE` has passed, and only while the Spark still runs
the exact rollback source. A later authenticated contact that proves the
target completes the order first. Package and semantic version strings, plus
any capabilities unknown to this Controller, are informational metadata; the
protocol version and the required upgrade capability remain safety checks.

## Retired state

`/var/lib/vonk-forge/package-upgrade/`, `package-upgrade.status` and
`package-upgrade.preflight.*` belonged to the retired forward capsule. Nothing
writes them any more; the helper's store retention removes them once unused
for a day.

## Proof

- `tests/nodes/test_agent_package_rollback_systemd.py` runs the built helper
  and its disposable acceptance probe on a systemd host. Both fixture package
  generations ship no `preinst`; the service executable and postinst are
  explicit fault-injection fixtures.
- `tests/nodes/test_agent_package_transition_systemd.py` starts from the
  promoted dev release package. It downloads the package named by the signed
  `release.json`, reseals it with a throwaway key, and drives the promoted
  helper's own units, sandbox and watchdog; signed grants go over the real
  socket through `package_grant_probe`. The candidate is this tree's package.
  Cases: a finisher upgrade with acknowledgement; a candidate that arrives
  with a capsule's staged files left by the promoted package; a failing
  postinst; dpkg killed while its payload extraction is held (`iHR`); the
  helper cgroup killed, and a reboot stand-in, while the candidate's postinst
  is held (`iF`). No capsule intent or arming breadcrumb may appear in any of
  them. A runner cannot reboot inside a job, so the stand-in kills and stops the
  package's units, waits past the deadline and starts the units a boot starts;
  a kernel boot and lost unsynced writes are not reproduced.

This is Debian/systemd acceptance on disposable ARM64 runners, not physical
Spark, NVIDIA, or live Controller deployment evidence.

## Retry behavior and diagnostics

Package configuration first checks whether rollback service enablement already
exists. An upgrade does not rewrite that Debian state inside the helper's
read-only sandbox; a fresh install still enables boot recovery. This follows
[Debian's idempotent maintainer-script requirement](https://www.debian.org/doc/debian-policy/ch-maintainerscripts.html#maintainer-scripts-idempotency).

After restoring the captured source, the watchdog retries service activity,
MainPID, and the actual executable digest for up to 15 seconds of polling.
A transient startup observation does not immediately trigger another reinstall.
The expected source digest remains mandatory. Failed installation still transfers
control to rollback, which restores the source.

The helper streams dpkg stdout and stderr to its service journal while retaining
a short diagnostic tail. Inspect `journalctl -u vonk-forge-package-helper`
for the package configuration error; the API's bounded failure code does not
contain the full journal. This logging improvement takes effect after the new
helper is installed and cannot recover output discarded by an older helper.

Both installation and rollback stream dpkg output into their service journals.
The install rejection additionally carries an optional bounded diagnostic in the
canonical helper response. The agent sanitizes it before publishing the failure
reason and diagnostic log tail, so the existing Controller job UI can show the
cause without requiring SSH.

Timed-out dpkg commands run in a dedicated process group. The helper terminates
that group and reaps dpkg before permitting rollback, preventing child maintainer
scripts from continuing package mutations after their parent times out. Output
is continuously drained; only the retained diagnostic tail is bounded, not the
amount a package is allowed to print.

Activation receipts are root-owned, non-secret evidence read by the agent. The
writer explicitly sets mode 0644 before atomic publication; creation mode alone
is insufficient under the services' 0077 umask. Private rollback transactions
remain mode 0600 inside a mode-0700 directory. The receipt regression test runs
in a child process with the service umask, without changing the test runner's
process-wide permissions.

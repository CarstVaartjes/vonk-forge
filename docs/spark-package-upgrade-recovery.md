# Spark package upgrade recovery

Controller-managed Spark upgrades use a single root-custody protocol. The agent
downloads both the complete signed candidate and the captured installed source DEB into its private incoming directory, but the
already-installed signed package helper never passes that agent-owned path to
`dpkg`. It opens the source without following links, validates the artifact,
copies those exact bytes into a fresh root-only
`/run/vonk-forge-package-candidates/<invocation>/` directory, and invokes `dpkg`
with that custodied path. The durable pre-install script accepts only that path
shape with exact root ownership, modes, link count, invocation grammar, digest
filename, package fields, and payload identity before taking its durable cache
copy. There is no direct agent-owned candidate or package-helper bridge path.

Once the new pre-install script starts, it commits the exact signed package,
recovery runner, static boot trigger, and agent-start gate before publishing the
intent. The intent denies agent startup until exact activation evidence exists;
a following root-owned blocker records the pre-activation phase. Recovery
accepts only the canonical three-line schema-2 pending record, normalizes it to
the exact target, repairs only `vonk-forge-agent`, proves the
installed and running helper/agent identities, and compare-deletes the intent.
It uses no network and never runs an unbounded `dpkg --configure -a`.

If named-package configure or exact-cache install still fails, the recovery
runner may remove and reinstall only `vonk-forge-agent`. That fallback is
allowed only while all of the following still match: the root-owned 17-line
intent, target version and architecture, package and payload digests, recovery
nonce, recovery systemd cgroup, and the exact parent `dpkg` argv. The package
scripts preserve enrollment configuration, certificates, recovery gates, and
the root-owned package cache across that narrowly authorized remove. A newer
installed package is never removed or downgraded.

`/var/lib/vonk-forge/package-upgrade.status` is a root-owned, seven-line,
secret-free receipt containing only a bounded outcome, allowlisted stage,
bounded reason token, the target package-version metadata and package digest,
and bounded `dpkg` state. New helpers also return allowlisted package
verification, metadata, custody, or installation codes; only installation
failures may carry an exit status in the range 0 through 255. The Controller
treats every one of these detailed failures as recoverable but operator-paused.
It does not dispatch the next Spark until a new authenticated protocol-v3
contact proves the exact signed package, binary digest, build digest,
architecture, required upgrade capability, and successful self-test, followed
by the exact activation acknowledgement described below. Package and semantic version strings,
plus any capabilities unknown to this Controller, are informational/open
metadata and are not required to match across Controller and Spark. The
protocol version and the required upgrade capability remain safety checks so
the receiving side can understand and execute the signed operation.


## Bounded source restoration

Before `dpkg` can stop the healthy agent, the current helper verifies both DEB
signatures, all maintainer-script and watchdog executables, configured package
state, and the source package/version/agent/helper hashes. The prior package
must be the exact signed source captured by the Controller for this attempt;
there is no caller-provided package path or arbitrary downgrade request. The
source bytes and source helper executable are retained in a root-only durable
transaction directory. The fixed systemd watchdog uses that retained executable,
so replacing or stopping the installed helper cannot remove the recovery runner.
The unit is enabled by the current package and checked before activation.

The signed install operation carries one nested `PackageRollbackAuthority`:
source package digest/signature/version and agent/helper hashes, a fresh attempt
nonce, and a bounded activation deadline. A failed install triggers restoration
immediately. An absent readiness/reconnect acknowledgement triggers restoration
at the deadline. Restoration stops the exact conflicting package services,
retires only the matching candidate recovery intent, reinstalls only the captured
source DEB, and verifies configured package state plus the actual restarted
process executable. Interrupted restoration resumes from that same durable
transaction; it cannot select a different package or overwrite a third identity.

The source package's normal downgrade guard remains active. Its fixed read-only
rollback validator accepts the exact captured source only from systemd's
watchdog cgroup, while the transaction is `rolling_back` and its attempt nonce
matches. A copied nonce used from another process, another package, or another
attempt does not authorize a downgrade. Source installation runs with the
current maintainer scripts' offline activation setting; the watchdog itself
restarts and proves the source process afterwards. If the initial `dpkg` result
arrives while the exact candidate recovery capsule is still active, or after it
has already proved the candidate process, the rollback watchdog leaves the
transaction armed until the existing activation deadline. This gives the
root-owned capsule one owner for the repair and prevents the watchdog from
stopping it mid-configure. If recovery does not produce the exact candidate
package and process before that bounded deadline, the watchdog resumes source
restoration. After restoration, the source process identity proof polls the
service's `MainPID` for a bounded interval so a normal systemd restart race is
not reported as a failed rollback; it still requires the `/proc/<pid>/exe`
digest to equal the captured source agent.

The Controller issues the signed fixed `confirm-package-activation` operation
only for the candidate that has met the contact/readiness gate. It binds the
candidate package digest and original attempt nonce; the host grant binds the
node. The helper also checks the installed candidate executable hashes. A
successful contact alone does not cancel the watchdog or advance the canary.
The acknowledged root receipt is the final activation gate.

`/var/lib/vonk-forge/package-activation.receipt.json` is a root-owned readable
current schema-2 `PackageActivationReceipt`. It carries the node, source and
candidate versions/package and agent hashes, attempt nonce, phase, creation and
update times, and bounded outcome. It contains no arbitrary shell output,
credentials, package paths, or signature private material. The Controller can
report `armed`, `activation_failed`, `acknowledged`, `rolling_back`, `rolled_back`,
or `rollback_failed` without confusing candidate contact with activation success.

The ARM64 systemd CI lane executes
`tests/nodes/test_agent_package_rollback_systemd.py` with the built current
helper and its disposable acceptance probe. Both fixture package generations
use the current signed protocol and current preinst; no obsolete supported
baseline is invented. The small service executable and postinst are explicit
fault-injection fixtures, while adjacent recovery/repair shell lanes exercise
the complete production maintainer scripts. This is Debian/systemd acceptance,
not physical Spark, NVIDIA, or live Controller deployment evidence.

# Spark package upgrade recovery

Controller-managed Spark upgrades cross one unavoidable bootstrap trust boundary.
The already-installed, signed package helper validates the controller-provided,
digest-named DEB and then invokes `dpkg` with that path. Because the dev335
protocol does not pass an already-open file descriptor, a compromised old agent
that owns the incoming directory could replace the path between those two opens.
The durable pre-install script narrows and revalidates the invocation, file
metadata, digest, package fields, and payload before taking a root-owned stable
copy, but it cannot retroactively eliminate that old-protocol open-by-path gap.
Recovery from dev335 therefore assumes the currently installed signed
agent/helper is not compromised. Physical recovery requires an out-of-band
reinstall if that trust assumption is not valid. The root-custody helper
protocol closes this gap by copying the verified file through the same open
descriptor into the dedicated root-owned
`/run/vonk-forge-package-candidates/<invocation>/` runtime directory;
pre-install
accepts that custody path only with exact root ownership, modes, link count,
invocation grammar, and digest filename. Until a helper implementing that
protocol is installed, the dev335 bootstrap trust assumption still applies.

Once the new pre-install script starts, it commits the exact signed package,
recovery runner, static boot trigger, and agent-start gate before publishing the
intent. The intent denies agent startup until exact activation evidence exists;
a following root-owned blocker records the pre-activation phase. Recovery
accepts only canonical legacy pending records,
normalizes them to the exact target, repairs only `vonk-forge-agent`, proves the
installed and running helper/agent identities, and compare-deletes the intent.
It uses no network and never runs an unbounded `dpkg --configure -a`.

If named-package configure or exact-cache install still fails, the recovery
runner may remove and reinstall only `vonk-forge-agent`. That fallback is
allowed only while all of the following still match: the root-owned 14-line
intent, target version and architecture, package and payload digests, recovery
nonce, recovery systemd cgroup, and the exact parent `dpkg` argv. The package
scripts preserve enrollment configuration, certificates, recovery gates, and
the root-owned package cache across that narrowly authorized remove. A newer
installed package is never removed or downgraded.

`/var/lib/vonk-forge/package-upgrade.status` is a root-owned, seven-line,
secret-free receipt containing only a bounded outcome, allowlisted stage,
bounded reason token, exact target version and package digest, and bounded
`dpkg` state. New helpers also return allowlisted package verification,
metadata, custody, or installation codes; only installation failures may carry
an exit status in the range 0 through 255. The Controller treats every one of
these detailed failures as recoverable but operator-paused. It does not issue
an automatic second mutation and does not dispatch the next Spark until a new
authenticated protocol-v3 contact proves the exact binary digest, build
digest, semantic version, required capabilities, architecture, and successful
self-test.

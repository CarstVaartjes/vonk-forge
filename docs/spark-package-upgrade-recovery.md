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

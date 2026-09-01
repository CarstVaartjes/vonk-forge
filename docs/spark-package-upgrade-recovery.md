# Spark package upgrade recovery

Controller-managed Spark upgrades use a single root-custody protocol. The agent
downloads the digest-named DEB into its private incoming directory, but the
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

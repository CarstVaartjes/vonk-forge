# Documentation guide

The repository rules for agents are in [AGENTS.md](../AGENTS.md); the full text
of its long sections is [agent-rules.md](agent-rules.md).

The [engineering principles](engineering-principles.md) and
[architecture overview](architecture-overview.md) own current architectural
direction. Keep state ownership, lock order, and waiting behavior explicit.
PostgreSQL owns coordinated control decisions and security; the target managed
storage boundary owns artifact availability and local recovery records. Never
describe both SQL and JSON as independently authoritative for the same fact.

Deadlock prevention is a required architecture property. Document who owns each
lock, slot, and reservation, the allowed acquisition order, what waiting work
releases, and how it resumes. Preserve bounded nonblocking coordination, child
lease inheritance, stale-attempt fencing, and failure isolation. Do not claim
that documentation or a timeout alone proves current code cannot deadlock.

Distinguish target contracts from implemented behavior
and tested/deployed evidence. A reader's tolerance of a newer recipes minor is
evidenced by the future vector gate ([testing-and-ci.md](testing-and-ci.md#release-acceptance-before-merge)). Keep operator commands accurate until the matching
implementation ships. Remove superseded architecture instructions and update
their references; retain useful product rationale with an explicit historical
scope rather than letting it override the current architecture.

Error handling and diagnostics must follow [error-reporting.md](error-reporting.md).
Document operation, endpoint path, received status, safe canonical code,
request ID, source classification, and retry/defer/exit decision when those
values are available. Never document credentials, private keys, cookies,
authorization headers, token-bearing URLs, or raw request and response bodies.
Document the public-contracts pin as a recipes release's attested wheel, verified once at
ingress by `scripts/dev/refresh-public-contracts`; never as a recipes git revision. The
acceptance library revision is a separate pin.

Keep repository, Controller, and physical Spark evidence boundaries explicit;
do not claim route-wide or Rust coverage until the owning producer and
consumer tests have passed.

For Fleet profiles, document the local NAS/Controller cache as the authority
for profile choices and apply admission. Choices resolve to exact cached model
and recipe-image identities; Spark-local copies are execution caches only.
Missing assets must remain visible as actionable blockers with a prepare-cache
action. A ready apply prepares exact assets on target Sparks in parallel,
skips assets already local, safely stops and replaces workloads, and reports
durable progress. The NAS removes unused local model-cache entries while
preserving referenced entries. Treat the managed local cache as trusted and do
not promise repeated full hashing. Preserve the current schema-2, single-path
contract: do not document legacy fallbacks or compatibility aliases.

Document the whole-fleet membership rule too. Distinguish saved drafts
and one-shot load applications from one durable selected-profile record in
PostgreSQL that points to the immutable accepted application snapshot. Do not
infer it from latest application history. The Controller worker reconciles
that snapshot after enrollment or revocation, without a new operator decision:
new Sparks are idle; a removed Spark's independent assignment is ignored while
the snapshot stays intact. If removal leaves a multi-Spark model without a
required member, stop and confirm its reachable ranks, withdraw the model route,
report the missing member, and continue unrelated assignments. Do not count an
old run as live capacity or claim an unreachable Spark stopped; reconcile a
same-ID rejoining Spark against the accepted snapshot before scheduling it. A
review binds current membership, so changes before acceptance require a new
review.

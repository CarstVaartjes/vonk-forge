# Documentation guide

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

The [storage and coordination plan](plans/resilient-artifact-storage.md) tracks
pending implementation. Distinguish target contracts from implemented behavior
and tested/deployed evidence. Keep operator commands accurate until the matching
implementation ships. Remove superseded architecture instructions and update
their references; retain useful product rationale with an explicit historical
scope rather than letting it override the current architecture.

Error handling and diagnostics must follow [error-reporting.md](error-reporting.md).
Document operation, endpoint path, received status, safe canonical code,
request ID, source classification, and retry/defer/exit decision when those
values are available. Never document credentials, private keys, cookies,
authorization headers, token-bearing URLs, or raw request and response bodies.
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

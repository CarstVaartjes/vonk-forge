# Controller due-wait redrive

The Controller worker observes persisted waits independently of API requests,
agent claims and heartbeats. `DueWaitBackstop` uses the existing owner tables;
it stores no scheduler rows, leases or additional due timestamps.

The selection clock is each model's `next_action_at` attribute. Existing SQL
aliases remain: RecipeRun uses `route_next_attempt_at`, RoutePublicationOwner
uses `reconciliation_next_at`, AgentCertificate uses `ca_next_attempt_at`, and
AgentIssuedCertificateRevocation uses `next_attempt_at`. Terminal state does
not exclude a row whose wait still owns unresolved cleanup.

`wait_redrive.wait_sources` binds the current owners:

| Owner | Normal advancement used by redrive |
| --- | --- |
| AgentOperation | Order reconciliation and lifecycle settle with compare-and-save |
| AgentUpgrade | Rollout healing and accepted-package advancement |
| ArtifactJob | Exact order/effect projection |
| FleetProfile / selected profile | Application cancellation, admission, recovery and phase advancement / selected roster reconciliation |
| ImageAvailability / RecipeUpdateBatch | Existing fenced cancellation, removal and bounded claim executors |
| PrebuiltImage | Cancellation reconciliation and the pinned-image importer |
| Job | Registered handler and fenced queue claim |
| ModelCache | Cancellation, removal and bounded transfer executor |
| RecipeOperation | Exact cleanup and the receipt path's shared phase/terminal advancement |
| RunSwitch | Shared per-row advancement, wait recording and ended-Start cancellation |
| Route publication / recovery | Existing route maintenance and exact-run recovery/publication |
| Storage relief / cleanup | Existing guarded removal and residue owners |
| Catalog sync | Shared automatic observation and durable retry writer |
| Certificate / revocation | Existing remote revocation confirmation |

A scan examines at most sixteen rows and yields between handlers when its
one-second observation budget is spent. It rotates across tables and primary
keys, moving its cursor before invoking a handler. Busy or corrupt rows cannot
hold the first batch indefinitely. Cursors and scan cadence are disposable;
the owner's persisted clock remains the scheduling authority. The worker caps
scan frequency at once per second, including under continuous ordinary work.

Selection releases its SQL session before calling an owner. Owner advancement
re-reads current intent and uses the existing admission lock order, fencing and
transport deadlines. Cancellation or newer accepted intent wins before retry.
A malformed wait or handler exception records the table, row and cause, and
ends only that row's turn. An examined row is not reported as completed work.

Whole-tree guards compare concrete lifecycle adapters, canonical wait kinds and
ORM wait owners with the dispatch bindings. PostgreSQL tests cover worker death,
quiet reconstruction, corrupt-row and owner rotation, connection release,
cancellation, terminal cleanup and phase advancement.

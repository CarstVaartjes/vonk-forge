# Controller observation response budgets

The standalone CLI allocates at most `MAX_CONTROL_DOCUMENT_BYTES + 1` response
bytes before refusing an ordinary JSON response. Its 1 MiB bound is a reader
allocation backstop, not an admission rule or a promise every Controller route
has already satisfied. Request body limits do not establish response limits.

## Operation pages and details

`GET /api/operations` retains its requested item limit, filters, newest-first
`created_at/id` ordering, signed continuation, and authoritative total. The
producer measures compact UTF-8 JSON for each detail and the complete response
envelope, including the actual signed cursor. It selects the largest contiguous
prefix within the existing CLI reader allocation. It does not skip an oversized
row, reduce the total, or change an accepted operation. A shortened page's cursor
is issued at its last returned original row using the existing order and filter
context. Item bytes are measured once and summed; page selection is linear in
the candidate count rather than repeatedly serializing growing pages.

An indivisible optional progress or cancellation decoration may itself exceed
that allocation. The producer retains identity, actual kind/state/attempt,
parent/owner, node membership, and unaffected evidence. Only the oversized
optional fact reads as null, accompanied by a canonical `OperationProjectionIssue`
identifying the unavailable field, reason, observed full response bytes, and
reader budget. CLI and browser show that cause. This says the fact is unavailable
in this observation; it does not say no progress occurred, no cancellation was
requested, or that an execution outcome is uncertain. A later smaller fact is
read normally without a state repair or operation rewrite.

The installed-CLI regression uses twenty independently valid small persisted
reports whose complete ordinary list exceeds 1 MiB. Its indivisible case uses
the owning `aggregate_progress` constructor with independently valid member
facts. It does not claim an oversized single heartbeat crossed the ingress cap.
The test follows every signed continuation through the actual TLS API and
installed package, then exercises typed unknown and recovery with known durable
identity/state retained. These large proofs run in hosted CI.

Only proven operation list/detail and existing byte-sized model/recipe Library
page routes declare `x-vonk-response-max-bytes` in their canonical OpenAPI
operation. Consumers may stream-check that owner limit before appending body
bytes. This metadata is not an API-wide limit. A browser transport must not
infer an outgoing response bound from an incoming request cap or a schema item
count. The existing CLI's bounded `read(limit + 1)` protects the real installed
HTTP path; mocked/generated transports with preallocated bodies are not proof
of bounded network allocation.

## Whole-state contracts still requiring a decision

`GET /api/fleet` returns one authoritative `FleetSnapshot`: its event cursor,
revision, and complete node collections belong together. Fleet SSE initial/reset
frames also contain that entire snapshot. Event replay batching (128 events) is
not a snapshot byte limit. Silently omitting nodes, clearing installations, or
clamping a plan to meet a reader limit would change meaning and is prohibited.

`GET /api/platform` returns the API's own packaged provenance and all fresh
process-bound worker observations. A subset can conceal mixed deployed worker
versions. Its observation time and complete fresh membership must remain bound;
unknown membership cannot be represented as a smaller successful worker set.
Neither route has a complete serialized-byte promise. API/worker deployment
configuration supplies no process memory limit from which to derive a new
universal body number. The current CLI may reject a legitimate large response;
this remains a separate contract gap.

A reviewed next contract can either page complete immutable snapshots under a
single snapshot identity/revision (including explicit continuation/completion),
or stream typed snapshot records with a final completeness receipt. A client
must not make fleet placement or mixed-worker compatibility decisions from a
partial snapshot. Time, retention, streaming chunk allocation, and exact snapshot
identity need producer owners before introducing a numeric body/frame limit.
The existing 18 MiB agent claim allocation is justified by its 16 MiB compiled
plan plus 2 MiB assignment envelope; that exact contract is not evidence for an
18 MiB administrative fleet/platform budget. Replanning accepted work to fit a
transport response is not an alternative.

The operation change bounds outgoing reader documents. It does not prove a
universal Controller process memory ceiling: source query materialization,
concurrent requests, JSON object amplification, and whole-state observations
still require their own accounting.

# Complete immutable observations over bounded records

Fleet and platform observations keep their canonical full payload models. Their
GET routes now capture one consistent observation before opening the network
stream and return `application/x-vonk-observation+ndjson`. A required start record
binds a new transfer UUID and resource. Ordered base64 chunks carry canonical
UTF-8 JSON bytes. The complete record binds the chunk count, exact byte count and
SHA-256. A typed terminal error, disconnect, mismatched receipt, trailing record,
invalid UTF-8 or invalid canonical payload leaves the observation unavailable.
HTTP 200 alone is not a completed observation.

Every NDJSON record, including its newline, is limited by the existing CLI
1 MiB individual document allocation. OpenAPI owns the record budget and payload
schema through `x-vonk-response-record-max-bytes` and
`x-vonk-observation-payload`. Both Python and browser readers verify the full
receipt and EOF before publishing the original payload model. A single node's
membership can span multiple records; no domain fact is clipped or independently
published. Hosted proof exports real Python-produced small and larger-than-1-MiB
indivisible Fleet payloads into the browser reader, including a signed-BIGINT
cursor that JavaScript must preserve exactly.

Fleet SSE initial/reset frames contain refresh notices rather than snapshots.
Notices retain the last completed roster and applied cursor. A new timeline fences
older in-flight captures. Telemetry while a capture is required raises the
follow-up cursor; it does not patch a roster across an unobserved gap. Only a
verified complete capture can adopt a lower cursor after a database timeline
reset. Failed captures keep the completed roster with an unavailable cause and
retry through the existing bounded refresh pacing.

SSE additionally declares `x-vonk-response-frame-max-bytes` using the same 1 MiB
individual transport owner. The producer counts actual compact UTF-8 framing and
JSON before appending to its output buffer. Oversized saved optional event
bookkeeping becomes a bounded refresh notice with its real cursor, a typed cause,
and a measured byte lower bound above budget. An unencodable scalar has an
encoding-unavailable cause and no invented byte measurement. Healthy outbox
writes already enforce an 8 KiB payload limit; the oversized regression corrupts
a stored row after a normal write and preserves it through same-cursor repair.

The browser uses a fetch stream with the schema-derived frame budget before
retaining or decoding a line. It implements the WHATWG SSE field/delimiter rules,
including CRLF/CR/LF, one leading BOM, multiline data, persistent/empty IDs,
server retry values and incomplete EOF discard. The Control contract additionally
refuses invalid UTF-8 rather than replacing it with new text. One connection
owner sends cookies with same-origin credentials and reconnects from the cursor
actually applied to domain state, never merely the last received refresh notice.
It preserves long server retry delays through cancellable browser timer chunks.
Disposal aborts the active fetch and pending reconnect; 204 stops reconnecting,
and 401 invokes the existing authentication owner. Protocol/error recovery keeps
snapshot polling and completed-state authority separate from the stream.

These budgets bound an individual application transport buffer, not the aggregate
snapshot, model, SQL query, serializer temporary or concurrent process memory.
JSON scalar encoding and browser fetch chunk allocation are separate resource
owners. Full model validation still uses memory proportional to the observation.
No aggregate heap ceiling, complete query-memory bound or arbitrary roster count
is claimed. The new transport, generated clients, shared payload validators and
signed installed CLI must ship as one compatible source graph; old JSON-only
clients refuse the new media type. There is no runtime legacy fallback. The
accepted rollout must separately prove an installed-client compatibility
transition before changing the deployed Controller transport.

Parsing reference: [WHATWG server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream).

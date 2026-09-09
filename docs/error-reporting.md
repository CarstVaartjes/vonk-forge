# Safe error reporting policy

This document defines the current error boundary for Vonk Forge clients,
agents, helpers, and the Controller. It is a diagnostic contract: it adds
context without changing authorization, retry, rollout, or recovery semantics.

Every surfaced error has these fields when they are available:

* `operation`: the stable operation name, or the HTTP method plus route when no
  operation name exists;
* `endpoint`: the URL path only. Query strings, fragments, userinfo, and
  origins are excluded;
* `http_status`: the received HTTP status, or absent when no response arrived;
* `code`: a safe, canonical lower-case Controller or local code;
* `request_id`: the server generated or caller supplied correlation ID. Missing
  IDs remain absent; callers must not invent one after a failure;
* `source`: `remote_rejection`, `transport`, `local_io`, `protocol`, or
  `unknown`;
* `decision`: `retry`, `defer`, or `exit`, with `retryable` and recovery hints
  derived from the existing contract.

The public error text is bounded and redacted. It never includes request or
response bodies, bearer or basic credentials, cookies, authorization headers,
private keys, token-bearing URLs, or arbitrary exception representations.
Transport errors retain a reliable source classification: `dns`, `connect`,
`tls`, `timeout`, `body`, or `protocol`. An unrecognised exception remains
`unknown`; it is not guessed from a generic `OSError`. Local I/O includes the
safe operation/path and errno when available. A path is included only after
the caller has validated it as an operational path and never includes secret
file contents.

Controller responses carry `X-Request-ID` on every response and
`X-Vonk-Error-Code` on failures. The client reads both headers in addition to
the typed JSON problem fields. This keeps early middleware failures
correlatable even when their response body has no detail field.

HTTP 401 means authentication is absent or invalid. HTTP 403 means the
request was rejected after authentication; the middleware uses the generic
`controller.request_rejected` code because it cannot safely infer whether the
cause was role or CSRF validation. Domain producers may provide a more exact
canonical code. These responses remain terminal for the existing retry policy
and carry a safe code and request ID. Controller middleware must return the
typed error envelope for all API errors, including early authentication
failures and body-limit responses; no blank response is a diagnostic contract.

## Current implementation and ownership boundaries

The Python `ControlClient` now owns context construction and transport
classification. Its generated OpenAPI path identifies the endpoint, while
response headers and the typed problem object provide status, code, and
request ID. The CLI renders this context and keeps the existing retry/defer
behavior. The Controller request boundary generates and propagates an ID,
adds a safe code header, and logs unexpected route exceptions with the safe
operation/path and request ID. Unexpected route exceptions are returned through
the typed public envelope with the same correlation ID.

The Rust agent `ClientError::Controller` now preserves status, safe code,
path, request ID, and retry/exit decision for non-success responses. Its
transport variant exposes only reqwest's reliable broad classification and a
path-only endpoint accessor; it leaves uncertain DNS/TCP/TLS distinctions
unknown. `RotationError` preserves the nested client status/code/decision and
safe local identity or issued-credential classification. The helper request
protocol and operation rejection boundary map each handled failure to a stable
error code and bounded detail; raw I/O paths, command output, and credential
material are never emitted. Unexpected Controller route exceptions return the canonical
nested `BoundedErrorResponse.context` and the same request ID used in the safe
structured log. The OpenAPI document and generated Python/TypeScript clients
were regenerated from these Pydantic models. Other agent lanes retain
ownership of pairing, health, telemetry entrypoint/systemd, and transaction
error enums; their changes must preserve this contract before integration.

## Implementation plan

1. Use `ErrorContext` in the Python client for generated and direct HTTP
   requests, preserving exact status, safe code, request ID, and source.
2. Classify `urllib`/socket/SSL/timeouts only where the exception proves the
   class; preserve `unknown` otherwise.
3. Keep error rendering bounded and use the existing redaction helper for
   remote detail and local exception text.
4. Add representative tests for DNS/connect/TLS/timeout, 401 versus 403,
   request-ID propagation, decision values, and redaction. Tests must exercise
   the exception boundary rather than assert implementation source text.
5. Keep pairing, health, telemetry entrypoint/systemd, and transaction error
   boundaries on the same fields as their owning lanes land; regenerate
   OpenAPI and typify output from canonical Pydantic models whenever a wire
   shape changes.

This change deliberately reports the current coverage and leaves the Rust and
Controller route-wide integration gaps visible until their owners land the
corresponding producer/consumer changes.

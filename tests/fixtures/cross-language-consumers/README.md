# Real consumer corpus

`control/tests/cross_language_consumer_corpus.py --output DIR` emits `corpus.json`
and canonical validation `schemas.json`. Neither generated schema nor document
acceptance is hand maintained: both come from current owning Pydantic models.
The manifest contains raw JSON text, canonical acceptance and normalized text,
explicit consumer scopes, and real HTTP route/status envelopes where available.
Numeric spelling is preserved in input strings. No global integer digit limit is
disabled.

The dedicated hosted workflow compiles `consumer_corpus_probe` and runs production
Rust parsers, the real HTTPS agent client, and `StateStore` writer/reopen/replay.
Python tests additionally execute the actual ASGI app, generated HTTP client and
installed CLI process, preserving the same diagnostics leaf in AgentResult and
JobDetailResponse envelopes. HTTP 422 tests exercise the bounded response reader;
503 tests assert production status classification without claiming body parsing.

Browser hosted checks use the same emitted artifact through actual generic and
generated API clients. Components without HTTP metadata claim component validation
only. OperationProgress has no Rust Controller API parser; compiled plans and stop
payloads have no browser route. Aggregate diagnostics byte bounds and the Python
JSON ingress digit limit are explicitly narrower ingress constraints, not silently
claimed as structural JSON Schema behavior.

`scripts/tests/prepare_official_schema_consumers.py` requires the official
JSON-Schema-Test-Suite commit `5b0ee1613e45fcc2bddac00e07c19cd49b00d8a8` and selects
its draft 2020-12 type, required, properties, additionalProperties, numeric bounds,
multipleOf, enum, const, allOf, anyOf and oneOf files. It uses the production schema
compiler in mathematical integer/exact decimal mode. Production strict integer
lexemes and finite float rules are covered separately. No local Rust build, web
build, type suite, PostgreSQL or container is required for preparing these sources.

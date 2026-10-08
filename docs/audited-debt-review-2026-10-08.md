# Audited debt review, 8 October 2026

This is repository evidence for the auditeddebt track, based on the current
allowlist and the 6 October audit. It does not establish deployment or physical
Spark acceptance. The named seven start/build sites were already classified as
`already-retried` at the track's base; they are not seven newly removed debts.

| Class | Decision and owning path | Guard or verification |
| --- | --- | --- |
| Queued Start, three sites | `StartMixin._start_once` hands unavailable installation, compiled plan and endpoint evidence to `StartMixin.start`. The finite admission schedule preserves the reviewed plan and request identity, releases each transaction and returns typed uncertainty on exhaustion. No new queue owner exists before acceptance. | Registered retry proof; hermetic request test covers recovery, exhaustion, fresh admission and security refusal. |
| Build-service wiring, four sites | Preview now owns its bounded retry directly. Build admission catches all typed uncertainty, including the unavailable service from `_start_build_in_session`; source checking and explicit retry already own bounded attempts. Each new attempt re-observes the same inputs; security errors escape immediately. | `BuildMixin.preview_build` and `BuildMixin.build` registered retry loops; hermetic request regressions failed before the change. |
| Destructive Stop | Keep `profile_stop_authority.security-edge` and exact Stop scope fences: an unavailable or substituted target cannot authorize stopping a different workload. Only the destructive command is withheld; uncertainty does not authorize route withdrawal or broader cleanup. | Existing exact-scope and retry guards; classifier regression distinguishes `RecipeStopAuthorityRefused` from bookkeeping uncertainty. |
| Integrity at ingress | Keep the route bundle's byte checksum and safe-path checks as security edges when trusting files for activation. Missing files, stale observation, an absent acknowledgement or a busy publisher remain bookkeeping, never proof that a working route should be withdrawn. | Existing route/content identity guards. The remaining `route_runtime.bookkeeping-debt` entries are not relabeled as security. |
| Publication identity syntax, three sites | Recategorize `_identity` as input validation: canonical UUID spelling and hexadecimal digest syntax are checks on caller input before locking or effects. These checks do not compare stored evidence, provenance or content bytes. Invalid input retains no owner; canonical input remains eligible immediately. | Hermetic invalid-input/fresh-input test plus the blocker site/count ratchet. |
| Lease lapse | `StaleAgentLease` is typed uncertainty, distinct from `StaleAgentFence`. Existing bounded launch/reconciliation paths expire attempts; `end_unobserved_order` ends obsolete authority and releases terminal-parent reservations without claiming the remote effect stopped or changing a route. Actual authenticated fence/certificate substitution remains security. | Classifier regression prevents security-sounding lease diagnostics being promoted to security. Real PostgreSQL queue/claim recovery remains a CI verification boundary. |

Debt falls from 214 to 211 solely through the three reviewed publication-input
sites. The queued-start and build-wiring groups remain zero debt in both
snapshots. Route bookkeeping debt falls from 25 to 22; its remaining sites are
explicitly retained as debt rather than concealed by an integrity label.

No schema or wire enum changes are needed: the paths use the existing
`UnknownOutcomeError`, `WaitReason`, and security/invalid-request contract
categories. No production operation or recipe-build implementation was changed.

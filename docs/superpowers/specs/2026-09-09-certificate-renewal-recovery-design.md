# Recoverable agent certificate renewal

## Problem

Certificate renewal crosses two durable stores: the Controller database and
the agent credential directory.  A process can stop after any request or file
rename, and an HTTP response can be lost after the Controller has committed.
The active identity must therefore remain usable until the Controller has
admitted the replacement and the agent has durably selected it.

The renewal flow also needs a bounded recovery path for an unactivated
certificate that was staged for a different pending CSR.  A stale staged
certificate is an untrusted alternative identity; it is retired locally and
revoked at the CA before a new certificate is issued.  Retiring an issuing
intent whose provider result is unknown is unsafe and remains a terminal
reconciliation case because there is no certificate serial to revoke.

## State machine

The agent stores one pending key and CSR as an atomic pair.  It is created once
when the active certificate enters its renewal window and is reused verbatim
until the exchange commits.  The Controller stores the same CSR fingerprint in
the rotation intent and in the staged certificate.

```text
ACTIVE(old, valid)
  | persist pending key+CSR
  v
PENDING(old, valid; pending CSR)
  | renew(pending CSR), authenticated with old
  v
ISSUED(old, valid; staged certificate)
  | persist generation directory + staged pointer
  v
STAGED(old, valid; new certificate)
  | activate(new certificate, prove possession)
  v
COMMITTED(new, valid)
  | publish active pointer, clear pending
  v
ACTIVE(new, valid)
```

Every arrow is replayable.  The renew response is identified by the durable
CSR and Controller request identity; an exact retry returns the same staged
certificate.  Activation is idempotent after the Controller commits it.  The
old certificate remains active while the staged certificate is being written,
activated, or selected.  The agent changes its in-memory client only after the
active pointer is durable.

On restart, the agent resumes from the first durable state it finds: pending
CSR, staged generation, or active pointer.  It never generates a replacement
key merely because a response or process was lost.  A staged certificate that
has expired is retired and the old active identity is retained.

Generation numbers are storage slots, not identity.  Before reusing an
existing slot, the agent compares the private key, leaf, chain, and metadata
with the requested material.  An exact match is a replay; a mismatch is moved
to a uniquely named replacement directory and the new files are written.  The
active generation is immutable and can never be replaced.  After a successful
publish, or after a new paired identity is durably installed, unreferenced
generated directories are removed while the active and staged pointers remain
the only selectable credentials.

## Conflict recovery

The recovery operation is authenticated with the currently active certificate
and carries the durable pending CSR.  It is accepted only for an active node
and a currently valid source certificate.  If the Controller has an unactivated
staged certificate for another CSR, it performs this sequence:

1. Lock the node and certificate rows, mark the conflicting staged certificate
   revoked locally, and commit that denial before contacting the CA.
2. Retry CA revocation using the staged certificate serial until it is
   confirmed.  The local `ca_revoked_at` marker makes this idempotent across
   response loss and restart.
3. Re-enter normal renewal with the same pending CSR.  The Controller creates
   one new intent and binds its provider request identity to that CSR.

The conflict operation never accepts a staged certificate whose CSR does not
match, never replaces the active private key blindly, and never revokes the
active source as part of recovery.  An issuing or manual-recovery intent with
an unknown provider result is not discarded: without a serial, automatic
retirement cannot prove that the CA no longer admits the identity.  It is
reported as a typed terminal recovery condition for reconciliation.

## Failure and admission rules

Transport failures and 5xx/429 responses are retryable with bounded backoff;
malformed responses, invalid CSR/key binding, expired candidates, revoked
nodes, and authentication failures are typed terminal conditions.  The agent
keeps its current client and control loop alive while the active certificate is
valid.  Once it is expired or revoked, normal admission rules deny it and the
agent must use the separate re-enrollment authority.

The Controller's canonical nested Pydantic request and response models remain
the wire authority.  Rust types are regenerated from the exported schema; no
legacy field names or fallback document shapes are introduced.

## Proof obligations

Renewal tests cover durable pending-key creation and exact reuse, response loss,
local staging, activation acknowledgement loss, pointer-switch restart,
matching retries, conflicting staged CSR recovery and CA revocation, expired
candidates, revoked nodes, and concurrent renewal.  Cross-language tests
exercise the canonical request/response model and the real serialize/store/load
path.  Linux container and PostgreSQL tests remain the integration evidence;
physical Spark acceptance is a separate gate.

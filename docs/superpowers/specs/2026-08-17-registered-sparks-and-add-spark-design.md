# Registered Sparks and Add Spark entry point

## Problem

The repository Fleet document currently contains projected node definitions.
The Fleet UI renders those definitions even when no Spark has enrolled, which
makes a fresh database appear to contain Sparks with messages such as “Node is
not registered” and “Timestamp not reported.” That is an implementation detail
leaking into the primary user-facing count.

The existing enrollment flow is deliberately explicit: an administrator
creates a short-lived, node-bound grant; the Spark submits its CSR and evidence;
the administrator approves the evidence; and the Spark collects and stores the
issued certificate. The UI needs a clear entry point to that flow without
turning a browser into a privileged remote installer.

## Approved behavior

### Fleet visibility

The visible Sparks/Fleet collection is registration-backed, not projection-
backed:

- A fresh or reset database with no non-revoked enrolled `AgentNode` records
  returns and renders zero Sparks, even if `inventory/fleet.toml` still has
  projected definitions.
- Each successfully enrolled, non-revoked Spark appears exactly once.
- An enrolled Spark may be offline or stale and still remains visible; its
  status explains that condition rather than removing it.
- Pending, unregistered, and revoked identities do not appear in the active
  Sparks collection. Pending enrollment remains visible in the enrollment
  review workflow, and revoked identities remain available in agent
  administration/audit views.
- Repository definitions remain authoritative for allowed node metadata,
  placement, and operational validation. They are not themselves counted as
  active Sparks, and this change does not silently edit the repository.

The HTTP fleet evidence response and the live Fleet stream use the same
registration-backed visibility rule so counts, cards, and acceptance evidence
cannot disagree. Existing repository metadata continues to enrich a visible
registered node; missing registration data is never represented as a fake
Fleet card.

### Add Spark entry point

The Fleet page gets a small, always-visible plus icon in the Fleet/Sparks panel
header. It has an accessible name and tooltip, “Add Spark,” and remains visible
when the list is empty.

Selecting it opens a guided enrollment entry point that reuses the existing
Agents workflow. The guide explains the sequence and links to the existing
grant/evidence approval surface:

1. Enter the Spark’s immutable node ID and create a one-time grant.
2. Copy the token once into the protected token file on the Spark.
3. Run the existing `vonk-agent pair` command on the Spark.
4. Review and approve the submitted evidence.
5. Run the same pair command again to collect the certificate, then remove the
   token file and start/restart the agent services.

The browser never attempts SSH, writes a Spark filesystem, or exposes a
long-lived credential. The Spark-side agent implementation remains unchanged;
it continues to generate the key/CSR and evidence, retry pending enrollment,
install the issued certificate, and delete the consumed token.

## Alternatives considered

1. **Filter only in React.** This would make the screenshot look correct but
   leave `/api/v1/fleet` and fleet evidence reporting unregistered rows to other
   consumers. Rejected because it preserves contradictory public semantics.
2. **Keep projected rows and relabel them.** This retains the confusing zero-vs-
   two count and forces users to interpret internal lifecycle states. Rejected.
3. **Use active registrations as the visible set while retaining repository
   authority for enrichment and validation.** Recommended and approved because
   it gives the user the expected count without weakening repository controls
   or inventing a remote-install mechanism.

## Implementation boundary

- Update the server-side Fleet projection and fleet evidence projection to
  exclude repository-only and revoked nodes from their public node lists.
- Add focused server regression coverage for a repository with projected nodes
  and no registrations, plus one- and two-registration cardinality.
- Add Fleet page/component coverage for the empty registered state and the
  accessible Add Spark control.
- Preserve the existing Agents grant, enrollment review, approval, and revoke
  APIs. The new control is navigation/guidance, not a second enrollment
  protocol.
- Update empty-state copy so it says no Sparks are enrolled and points to Add
  Spark, rather than saying the repository has no nodes.

## Acceptance criteria

- After the destructive development reset, Fleet renders zero Spark cards and
  no “unregistered node” cards.
- Adding one valid enrollment produces one Fleet card after the next stream or
  poll refresh; adding a second produces two, with no duplicates.
- Offline/stale enrolled Sparks remain cards with truthful timestamps/status.
- Revoked or pending identities do not contribute to the active Sparks count.
- The plus control is visible in both empty and non-empty states, is keyboard
  accessible, and has an accessible name of “Add Spark.”
- Existing enrollment and repository-authority tests continue to pass.


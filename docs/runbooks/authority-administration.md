# PostgreSQL authority administration

PostgreSQL is the sole runtime authority for fleet topology, platform policy,
recipe state, installations, placements, and runs. The control plane does not
mount or clone a Git repository and does not require a Git remote to operate.

## Inspect the authority

Use the browser Fleet and Library views for routine operation. The authenticated
API and `vonkctl` expose the same persisted projections for diagnostics and
automation. Every view identifies the immutable PostgreSQL authority revision
from which it was resolved.

See the [`vonkctl` local controller CLI](vonkctl.md) for connection setup and
the complete Fleet, Library, public catalog, and Activity command hierarchy.

## Change authority state

Changes are plan-first:

1. Read the current authority revision.
2. Create a proposal against that exact revision.
3. Review its typed changes, affected documents, and validation results.
4. Apply the proposal explicitly.
5. Follow the resulting reconciliation and audit records.

Proposals and immutable revisions are persisted in PostgreSQL. Applying a
proposal locks the current head, rejects a stale base revision, writes the new
revision, and advances the head in one transaction. Node identity uses the
certificate-bound `spk_...` identifier; hostnames and IP addresses are only
current observations.

Recipe and runtime work uses the Library workflow: resolve an exact recipe,
attach required evidence, map it to compatible enrolled nodes, preview the
operation, and apply it. Route publication is derived from accepted persisted
run evidence. It never reads a source checkout or an operator-supplied runtime
address.

GitHub remains the source and release build system. Its signed, immutable
artifacts enter the installation channel through CI, but commits and repository
files are not runtime desired state.

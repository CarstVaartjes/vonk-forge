# Control-plane operations

This guide is for the Vonk Forge web control plane. It is plan-first: review
the preview and resource evidence before applying an action. It is deliberately
written for the local fixture or the approved private control URL; never use a
live node as a test target.

## Fleet

Fleet is the authoritative operator view of each node. A node row joins the
repository fleet definition with authenticated agent evidence: lifecycle,
health, profile, agent activity, last-seen time, certificate expiry, capacity,
and compatibility. Live updates arrive over the Fleet stream; the browser
reconnects and falls back to polling when the stream is unavailable.

Open a node for telemetry history, installed recipes, loaded recipes, disk and
memory evidence, and recent operational messages. `Unknown`, `stale`, and
`missing` are honest states, not zeroes. Do not infer health from an absent
metric.

## Models, recipes, and nodes

Library is organized as Model → Recipes → Nodes. One model family may have
many recipe revisions or placements. A recipe can be unlinked, installed but
not loaded, loaded on one node, or available as a complete fixed-size group.
Multi-node recipes require the exact declared node count and rank group at the
same time; a partial group is never presented as runnable.

Install checks disk capacity. Load checks memory capacity and reservations.
The preview shows selected nodes, required and available resources, existing
install/load state, and any partial-failure or retry path. A bounded
recommendation is guidance, not an automatic placement decision.

Loading never implicitly unloads another recipe, and no action implicitly
stops a running workload. Coexistence is allowed when the preview proves it
fits. If it does not fit, the operator must choose an explicit, previewed
operation.

Every mutating action follows: review preview → cancel or apply → follow job
progress → inspect partial failures → retry only the failed operation. URLs and
back navigation preserve the selected model/recipe context. Advanced JSON is
for exact inspection or upload; after a valid upload the visual recipe detail
remains the primary view, while invalid input preserves the last valid view.

## Safe operator checklist

1. Confirm the target node identity and current last-seen/health evidence.
2. Select the model and exact recipe revision, including required node count.
3. Open the preview and verify disk, memory, reservations, rank assignments,
   and compatibility evidence.
4. Apply only the intended action; never assume a load unloads anything.
5. Watch the job and retry only after reading the failed-node reason.
6. Re-open Fleet and confirm installed/loaded state and telemetry freshness.

The control plane owns the plan and audit trail. Node agents perform accepted
operations over authenticated outbound connections; operators do not SSH to
perform routine lifecycle actions.

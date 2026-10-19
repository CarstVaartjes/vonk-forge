## Behavior contract (recorded before implementation)

Extend the existing Controller worker/reconcile pass. Persisted wait evidence and the owner's existing `next_action_at` attribute are the eligibility authority (including the existing route/certificate SQL column aliases). No new schema or universal job table.

- AgentOperation → the existing order reconciler/settle path; AgentUpgrade → rollout advancement; ArtifactJob → adapter projection; FleetProfile → cancellation/admission/recovery/ordinary advancement; ImageAvailability and RecipeUpdateBatch → their existing fenced claim runners; PrebuiltImage → the existing importer claim/run; Job → the existing fenced worker claim/handler; ModelCache → its existing cancellation/removal/transfer claim; RecipeOperation → existing Stop/build/retirement reconciliation; RunSwitch → existing `_advance`, wait recording and ended-start cancellation.
- Non-adapter owners: selected-profile → selected roster reconciliation; route publication/recovery → existing maintain/publish/recovery paths; storage relief/cleanup → existing gate/residue/removal reconciliation; catalog sync → existing automatic sync; certificate/revocation → existing remote confirmation. Every persisted wait owner must be represented, with whole-tree coverage that rejects an added unregistered owner.
- A bounded pass rotates across owners and stable row identities, advancing its cursor even for a busy/corrupt row. Selection releases its SQL session before invoking the owner. Owner functions retain the current lock order, fencing and exact effect identities, and release locks before I/O. One handler exception is reported with owner/row/cause and cannot suppress other rows.
- Each handler reloads current authority. Cancellation/newer accepted intent wins before retry. A terminal request remains terminal while its exact accepted cleanup is observed through the current cleanup owner; a due wait never authorizes a broader effect.
- A restarted quiet worker redrives due waits without new requests, claims, heartbeats or API reads. Real PostgreSQL fault injection covers reconstruction, cleared dependencies, starvation by a corrupt first row and cancellation before retry. No count ledger.

## Validation

Pending implementation and required local/CI gates. Auto-merge stays OFF. Production untouched.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01NxX8uY9ruX1ZqmuABxwaSa

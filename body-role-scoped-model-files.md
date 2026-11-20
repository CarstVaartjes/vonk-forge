## Summary
A recipe such as glm-5-3-flash-exlr8-kindlingai-gr8-dual selects different files per role (shared files for entrypoint and worker, `rank0-*` for entrypoint only, `rank1-*` for worker only). The whole set went into every node's compile and delivery check, so compile refused ("verified cache objects do not exactly cover the selected model files") and delivery would have refused with MODEL_SET_IDENTITY_UNAVAILABLE.

Rule: a node's model files are the files the recipe selects for that node's role. Note the cache manifest's `roles` are model-file tags (`weights`, ...), not recipe selector roles, so the role scope comes from the recipe selectors.

- `execution_plan_service._role_model_objects`: one place filters the receipts per node by the recipe's selectors for its role, before the coverage check and bytes sum.
- Delivery: `distribution_executor` builds each node's assignment from that node's own compiled plan (not the first plan for all); the set byte total is the union across plans.
- `verify_artifact_set` (all four sources): `contained_content` replaces `same_content`; every assigned object must be content (digest and length) the stored set holds. The stored set is the union over roles; exact role scoping is the node's plan (checked at compile), so containment is the same check.
- Deleted `same_content`.
- Test: two-role fixture with role-specific files, compile each role, delivery verification for each; a wrong-length object is refused.

## Principles
- Identity by content: receipts matched by (model digest, file id); delivery by digest and length; no paths or revisions.
- One owner: role scoping lives in `_role_model_objects`; plans own each node's objects; one containment predicate for all sources.
- No compat path: `same_content` deleted, no dual readers; no schema change.
- Fix the class: every `verify_artifact_set` implementation and the single-plan assignment source were changed.
- Upgrade safety: single-role recipes behave as before (same objects); no stored data changes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

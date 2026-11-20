UX plan B6 (finding C5). `vonkctl fleet` text output only; `--json` unchanged.

- Identical "Installed, not running" rows (same title, same Sparks) print once with `(xN)`.
- Default list shows 10 rows, then `+K more; vonkctl fleet --wide lists every installation` (existing `--wide` flag; it lists every installation with its id, ungrouped).
- Workload line says "not taking requests" etc. instead of "route withdrawn", using the contract `RouteState` enum (added to the generated CLI vocabulary).
- Runbook `docs/runbooks/vonkctl.md` updated; lean test added to `test_cli_wording.py`.

## Principles
Simple: one grouping Counter, one word map, no new flag. Contract-first: route words keyed on the generated `RouteState`, not literals. Delete over add: nothing new in JSON or API. No legacy path; wide keeps the old per-installation listing. Docs updated in the same PR.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01NxX8uY9ruX1ZqmuABxwaSa

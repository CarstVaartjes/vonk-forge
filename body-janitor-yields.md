## Summary
The image-janitor's ingress heal raced Compose's network swap (CI run 44647226715). After a heal the network carries `config-hash = "janitor-recreated"`, so the next `compose up` stops Caddy cleanly, disconnects it and removes the network. A janitor tick in that window counted the missing declared network as broken with no re-observation, recreated it, and Compose adopted it without reconnecting Caddy. Caddy started without ingress and `--wait` failed. The same race can hit a real NAS redeploy.

`broken()` now treats a missing declared network as broken only when `should_run(container, pending)` and `observed(container)` both hold (mirroring the detached-network path). Otherwise it returns None (WAIT_NETWORK). The `janitor-recreated` label stays.

- Tests (fake daemon): Compose's sequence (stop, drop from record, delete network) with a tick at each step creates and mutates nothing; a container that flips to exited mid-tick is not condemned; the fake now answers 304 for stopping an already-stopped container, and existing heal tests still pass.
- Acceptance `compose_startup_diagnostics` always includes image-janitor logs on a startup failure.
- Docs: deploy/compose/README.md janitor section. No AGENTS.md text describes the heal.

Local: janitor tests pass (41); check-added-lines clean. ruff not run (local 0.16.1 vs pinned 0.16.9).

## Principles
Self-healing that never fights Compose's reconcile: the janitor heals only what should be running and is still as observed; a cleanly stopped container is Compose's to reconcile. No new path, one stricter condition, newest state wins via re-observation.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01NxX8uY9ruX1ZqmuABxwaSa

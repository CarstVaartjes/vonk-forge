## What and why

## Lifecycle counts

Required when this PR touches lifecycle, raise or allowlist files
(`control/src/vonk_control/lifecycle/`, `rust/crates/vonk-agent/src/`, the files
in `tools/*allowlist*.json` and `tools/vocabulary-literals-baseline.json`, the
scanners under `control/tests/`). Generate the block with
`scripts/lifecycle-counts report --base origin/main` and paste it here; the
`Lifecycle counts` check compares it with main. Delete the block otherwise.

```
before: writers=? operator_waits=? raises=? debt=?
after: writers=? operator_waits=? raises=? debt=?
```

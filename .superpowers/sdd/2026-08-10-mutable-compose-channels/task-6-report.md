# Task 6 report: mutable Compose channel operator documentation

## Status

Completed in implementation commit `bfe0c01573e43501fd20904839a677f1d1bcb82c`
(`docs: document mutable compose channels`).

## Red evidence

Before rewriting the operator documentation, the required runbook contract
command failed with 4 failures and 5 passes:

```text
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest tests/runbooks/test_development_nas_installation.py -q
FFFF.....                                                                [100%]
4 failed, 5 passed in 0.03s
```

The failures identified the missing normal mutable `:dev` artifact wording,
mixed-cohort failure before migration, trusted-host-updater `stable` authority,
and schema-aware pinned recovery path.

## Green evidence

After the documentation and contract rewrite, the same command passed:

```text
.........                                                                [100%]
9 passed in 0.02s
```

`git diff --check` also completed without output before the implementation
commit.

## Self-review

- The development guide leads with the generic NAS/UGREEN two-item layout,
  uses bare mutable `:dev`, and makes normal updates an unchanged-file
  pull/redeploy rather than a restart.
- It documents the reporter/gate/initializer/migration order and retrying a
  mixed cohort without deleting secrets or volumes.
- The production README makes the trusted host updater the signed `stable`
  selector and confines `:latest` to evaluation/discovery.
- Pinned recovery distinguishes compatible-schema guarded repository-volume
  reset from incompatible-migration matching full-state restore or clean
  development reinstall. The existing actual-volume discovery, Compose-label,
  typed-confirmation, and single-volume-deletion guards remain covered.

## Concern

This is a documentation-contract verification only; it does not perform a live
NAS deployment or host-updater release selection.

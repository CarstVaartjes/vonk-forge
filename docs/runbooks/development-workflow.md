# Development checkout and pull-request workflow

The [agent guide](../../AGENTS.md#checkout-pull-requests-and-worktree-lifecycle)
sets the required checkout and merge policy. This runbook owns the operational
steps. These are repository operations; publication and deployment retain their
own authority and evidence boundaries.

## Keep the canonical checkout on main

`/opt/vonk-forge` is the canonical checkout of local `main`, tracking
`origin/main`. Git calls the primary checkout a worktree too; a separate linked
worktree for `main` is unnecessary. Feature work, integration, and subagents use
their own linked worktrees. Directory names do not establish branch identity.

Before updating, inspect the actual state:

```bash
git -C /opt/vonk-forge status --short --branch
git -C /opt/vonk-forge worktree list --porcelain
```

One coordinator updates the clean, idle canonical checkout before new work and
after merges. Once it is confirmed to be on `main`, run:

```bash
git -C /opt/vonk-forge fetch origin
git -C /opt/vonk-forge merge --ff-only origin/main
```

Fetching refreshes the remote-tracking ref; it does not move local `main`.
Fast-forward failure means there is divergence to investigate. Do not reset or
force-update the branch to hide it. If the checkout is dirty, on a feature
branch, or used by another task, preserve it and coordinate with that owner.
Finish or safely relocate the owned work before restoring the canonical
layout. Do not switch branches beneath a running agent or commandeer a linked
worktree holding `main`. A periodic pull that races active work is not the
meaning of keeping the checkout current.

## Start isolated work

Fetch, then create each independent task from that exact `origin/main`. Example:

```bash
git -C /opt/vonk-forge fetch origin
git -C /opt/vonk-forge worktree add -b codex/example-change \
  /private/tmp/vonk-example-change origin/main
```

Use a distinct path and branch for the actual task. Lead agents use worktrees
too. Name the owner, base commit, and overlapping files before delegating. An
explicitly coordinated dependency may start from an integration commit;
unrelated tasks still start from freshly fetched remote `main`.

Run commands, edits, tests, and commits in the owning tree. Keep environments
local to that tree and use writable task-specific caches. Never overwrite
another task's changes. Do not run a bare `git stash` or `git stash pop` in the
canonical checkout: its stash stack is shared with earlier tasks. A temporary
detached comparison worktree is preferable for before/after checks. Explicit
path stashes in an owned tree must retain and restore their exact stash identity.

## Integrate related agent results

When several subagents finish related work together, prefer one integrated PR
if it forms a coherent, reviewable outcome. One coordinator owns an integration
branch/worktree based on current remote `main`, brings in the scoped commits,
resolves overlaps deliberately, regenerates shared outputs, and verifies the
combined producer/consumer and recovery boundaries. Passing each child branch
separately does not establish that their combination works.

Keep the PR title and description about the final combined behavior, including
the relevant component ownership, checks, and remaining evidence limits.
Unrelated or independently reviewable work may remain separate. Do not bundle
unfinished changes just because their agents completed at the same time, and
do not merge both a component PR and its integrated replacement. Close
superseded component PRs with a link to the integration PR once their final work
is accounted for. Any schema change in the bundle retains the schema merge gate.

## Review, refresh, and auto-merge

Before committing, inspect `git status`, the complete diff, and
`git diff --check`; run the [relevant verification](../testing-and-ci.md).
Open the PR with base `main`. Use installed `gh` for GitHub operations and its
help for the installed command version. Preserve configured commit signing;
authentication or a locked signing agent is not permission to disable it.

When merge is authorized, prefer auto-merge for an eligible PR. Keep required
checks, reviews, and branch protection intact; never use an admin bypass to
make it eligible. Check existing auto-merge requests first and arm only one PR
at a time. Example for a reviewed eligible PR:

```bash
gh pr merge PR_NUMBER --auto --squash
```

Keep schema-changing PRs out of auto-merge. Follow the agent guide's
[schema decision boundary](../../AGENTS.md#schema-implementation-merge-and-deployment)
and carry existing explicit authorization forward. Implementing, merging, and
resetting a deployed database are different decisions.

An out-of-date branch is routine integration work, not a reason to stop at an
open PR. In its clean, owned worktree:

```bash
git fetch origin
git merge origin/main
```

Resolve source conflicts by preserving both intended behaviors deliberately.
Regenerate conflicting generated files with their current generators; never
hand-edit a digest map. Recheck the complete result, run affected tests and
`git diff --check`, commit the resolution if required, then push the branch.
Confirm the checks refer to its new head and that auto-merge remains enabled
when eligible. Do not force-push another agent's branch. If a review, permission,
or failed check still blocks the merge, report that specific condition.

## Serialize publication-producing merges

A merge triggers image production only when the actual workflow path filters
select it. Inspect `.github/workflows/dev-images.yml` and the producer results;
some scripts, workflows, and tests are build inputs. Do not assume a commit has
`dev-sha-<full-commit-sha>` merely because it reached `main`. Verify image tags
in the registry; `git ls-remote` verifies Git refs, not registry images.

For a build-producing merge, wait for an accepted generation whose relevant
artifacts contain that change before arming the next PR. The publisher rejects
superseded builds; several rapid merges can leave newer built images outside
the accepted channel. A floating alias follows accepted publication. An image
existing in the registry alone is not an accepted release or deployment proof.

A documentation-only merge outside all producer filters creates no image and
does not require a nonexistent generation. Do not classify it as a failed or
superseding build. If an earlier build-producing merge still awaits acceptance,
resolve that publication gate before arming another PR. Follow the current
[publication runbook](platform-release-publication.md) to reconcile an eligible
producer/generation; never promote a guessed tag for a tip that did not build.

## Generated artifacts and release evidence

Two artifacts are called the manifest:

- `inventory/sbom/manifest.json` is the curated file-to-digest map over declared
  inputs, with no `source_sha`. Regenerate it last through the
  [supply-chain procedure](supply-chain.md). No change is expected when curated
  inputs did not change. After incorporating remote `main`, regenerate affected
  inventory instead of choosing one side of a generated conflict.
- `install.vonkforge.ai/artifacts/dev/current.manifest` is the signed release
  document carrying version, source, generation, and release path. Scheduled
  re-signing can refresh `source_sha` without rebuilding artifacts. Check the
  build commit in the package version and the provenance of the particular
  image; reused components or separately rebuilt images may have different
  build revisions. A refreshed source field alone does not prove new code runs.

## Remove landed worktrees

Cleanup is part of completing an authorized merge, including each component
tree whose final work landed through an integrated PR:

1. Verify the PR is merged and record its merge commit. Fetch remote `main`.
   Confirm each tree's final head/diff is represented in the landed PR or
   integration result and contains no later unmerged commits. A squash merge
   does not preserve component commit ancestry, so ancestry alone cannot decide
   whether the work landed.
2. Stop or release the tree's agents and confirm no task still uses it. Inspect
   tracked, untracked, and ignored files for work or evidence worth retaining.
   Preserve required reports outside the disposable tree. Dirty or unmerged
   work remains in place with a named owner and deferred-cleanup reason.
3. From outside the tree, remove the clean, inactive worktree with
   `git worktree remove /path/to/task-worktree`. Never use force removal to
   bypass uncommitted or valuable ignored work.
4. Delete the landed local task branch only after the merge/diff check proves
   there are no extra commits. Try `git branch -d codex/example-change` first;
   if Git refuses solely because the verified merge was squashed, delete that
   exact verified branch explicitly. Preserve branches whose status is uncertain.
5. Fast-forward the clean, idle canonical `main` checkout and verify
   `git worktree list` no longer contains the removed trees. Report anything
   intentionally retained and why.

Stale worktree registrations are different from live directories. Inspect
`git worktree prune --dry-run -v` before pruning; confirm the path is truly
retired, not a temporarily unavailable mount. Pruning metadata is not a
substitute for checking and removing a real task worktree safely.

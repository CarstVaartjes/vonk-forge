# Launch consolidation

Updated 7 September 2026. Existing work is consolidated through
[PR614](https://github.com/CarstVaartjes/vonk-forge/pull/614) before new implementation
starts. No new worker branches or worktrees were created during this consolidation.

## Ownership and branch disposition

The sole platform integration branch is `codex/launch-run-operation-repair` in
`/private/tmp/vonk-forge-interface-integration`, targeting `main`. Root owns
integration, generated artifacts, final checks and publication. Worker branches
are merged whole. Unique uncommitted work is reviewed before a worktree is closed.

| Work | Worker tip | Integration |
| --- | --- | --- |
| Current Fleet CLI | `d8d807b7` | Whole merge `1fb16d35`; 130 checks pass. |
| Fleet telemetry and monitoring | `bd5f5ff3` | Whole merge `63981606`. |
| Required browser telemetry fixtures | `d64ad4c0` | Whole merge `541ab41b`; 216 browser tests and production build pass. |
| Current Activity/browser clients | `84527532` | Whole merge `22fafe16`. |
| Worker secrets and Compose wiring | `cdb34cee` | Whole merge `a4d96e2e`; 60 focused and 45 Compose checks pass. |
| Current API worker wiring | `3fd18604` | Whole merge `f82b91b8`. |
| Old graph module removal | `393c17b1` | Whole merge `14d88a1b`. |
| Mandatory pinned client generator | `c5618bfe` | Whole merge `4d979a0b`; current source regenerated at `6c52338f`. |
| Current command/queue contract | `7dbf6393` | Whole merge `642cd061`, including the approved repair of the newly introduced global revision callback. |
| Canonical compiler metadata and current fixture coverage | `077ae720` | Metadata merged at `db77809d`; complete worker branch integrated with 223 focused compiler/lifecycle/artifact checks passing. Remaining obsolete validator consumers are recorded below. |
| Unused topology bootstrap seed | `9e3abb6a` | Whole merge `6293df00`; 24 PostgreSQL, authority and model-cache checks pass. |
| Current route authority and LiteLLM contract | `34ad4b6f` | Whole merge `e65eb291`; 115 checks pass, including PostgreSQL and the packaged consumer. |
| Queue concurrency repair | `30b2aa9f` | Whole merge `7349d467`; 86 queue, upgrade and PostgreSQL checks pass. |
| Current queue transaction fixtures | `af2bb135` | Whole merge `deef4ce0`; 33 checks pass. |
| Authenticated API and heartbeat fixtures | `5f7284c4` | Whole merge `dbbf967f`; 136 API checks pass. |

Combined CLI and generated-contract verification passes 245 tests and 45
subtests. The full Controller run is the final integration gate; earlier focused
results do not substitute for it.

## Uncommitted and older work

The inventory examined 105 checkouts, including 30 registered worktrees. The
original `/opt/vonk-forge` checkout remains untouched. Its 23 tracked changes
and nine untracked design documents are accounted for in the integration:

- All nine design documents are tracked here. Six match exactly; the interface
  plan, progress record and contract improvements document have newer versions.
- The old UI edits are superseded by the current compact paired lists.
- Installer, publication, image alias, pinned Compose, bootstrap and acceptance
  changes are present in the current implementation, including refactored forms.
- The original deletion of the old qualification parser is represented by the
  current canonical Model/Recipe qualification implementation; deleting that
  replacement would lose current functionality.

The earlier route worker (`55c8e6d4`) and Sol copy (`f6759528`) contain identical
three-file patches. The coherent current route implementation supersedes them.
The install/start, open-PR-review and runtime-image-wire branches were reviewed:
their useful behavior is already present, with the current wire validation
retaining the stronger typed structure. These older dirty checkouts remain
preserved until publication is verified; ancestry alone is not used to discard
uncommitted files.

## Explicit remaining cleanup

The proposed 53-path catalog/harness deletion is not applied. The current
`validate-recipe-library` script still reads execution-harness metadata and
checks adapters and topology support, and packaging still includes those inputs.
Those consumers must first use canonical compiler metadata before the obsolete
files can be removed together. This is a recorded unfinished cleanup, not a
second supported authoring format or an unaccounted worker patch.

## API coverage audit

The real application registers 136 FastAPI routes: 115 have response models;
the remaining 21 are raw transfers, uploads, event streams, metrics or empty
responses. OpenAPI is derived from the real application and the generated web
and CLI clients consume it. The bounded enrollment parser derives its schema
from the same Pydantic request class.

The pipeline is implemented, but complete nested-contract coverage is unfinished:
compiled plans, uninstall recipe content, artifact job compiled contracts and
kind-dependent lifecycle results still use open dictionaries in some responses.
Several alternate JSON error responses are constructed directly instead of
serializing their documented Pydantic model. These are concrete follow-up items;
dynamic engine parameters remain deliberately extensible. See `api-contracts.md`.

## Publication and deployment boundary

The integration targets remote platform `main`, last verified at `cb6edee0`.
Local validation and commits do not imply a GitHub merge, Controller deployment
or physical Spark acceptance. PR614 records publication and CI on its final head.

Recipes are published on `main` at `d55389b5` (v1.0.5); there are no open recipe
PRs. All 92 Models and 85 Recipes pass the structural catalog check. Website
launch PR59 is merged; its remaining open PRs are unrelated dependency updates.
No NAS deployment, model download or physical Spark execution is claimed by
this source consolidation.

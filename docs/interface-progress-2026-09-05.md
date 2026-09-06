# Launch evidence ledger: interface and platform integration

Review date: 2026-09-05. This ledger records the evidence available at the
current integration head and keeps repository implementation, local checks,
CI, publication/deployment, and physical Spark acceptance separate. The
platform base is `codex/interface-integration` at
`8930f9fbef2bd3bb2889450bf0dfb46c0edbcd4e`. The detailed acceptance and issue
mapping remain in the [interface contract ledger](interface-contract-ledger-2026-09-04.md).

## Evidence boundaries

| Gate | Implemented in source | Locally verified | CI | Published or deployed | Physical hardware |
|---|---|---|---|---|---|
| Recipe authority | Canonical Model/Recipe schema 2 consumer and package/index closure are integrated in the platform. | Recipes `10db3c7a73a18319f81103448ddaeada50334bb2` (`v1.0.1`) is schema 2 with 92 Models and 84 Recipes; 79 Models are referenced directly by a Recipe and 13 have no direct Recipe. | Recipe release CI workflow `33991938855` is recorded for `v1.0.1`; it is not a CI result for this platform integration head. | `v1.0.1` is the current recipe release; platform integration has not republished it. | No model or recipe has physical acceptance from this ledger. |
| Public web | Public catalog pages consume exact publication snapshots; compact model/recipe lists are implemented in the web merge. | Web merge `7eb783d63c5ea87d2efea8834467ddcda52decfd` is the reviewed source identity. | Production workflow `33985962484` is the recorded CI evidence. | Cloudflare deployment `https://abd3b57b.vonk-forge-web.pages.dev` is the recorded web deployment. This does not prove Controller or Spark deployment. | None. |
| Controller catalog and Library | Canonical catalog persistence/projection and Library cutover are present in local integration (`fb627731`, `bce3a410`, `8930f9fb`). | P9 owner evidence for `8930f9fb` records 4 OrbStack checks passed and 3 PostgreSQL checks passed (`5168777d`). | No CI result is claimed for this integration head. | No Controller or NAS deployment is claimed. | None. |
| Profiles, NAS cache and Run/Switch (P5) | Local merges cover profile scope, NAS cache/progress, automatic Run/Switch composition and receipts (`4ab865e6`, `a25b5d38`, `1cdbe531`). | Focused local tests cover durable progress, cache identity and canonical execution projections. They do not establish a connected two-Spark journey. | No CI result is claimed for these local-only merges. | No Controller/NAS rollout is claimed. | No Spark run or cache transfer is claimed. |
| Runtime wire and OCI delivery | Runtime compilation and receipt validation are integrated locally, but the final single schema-2 workload wire to the Rust OCI importer remains pending. Pending agent work is `665916b1` plus `d2a3fa92`; neither is in `8930f9fb`. | Existing protocol/unit fixtures prove serialization and validation boundaries only. | No CI result is claimed for the pending wire/import work. | No workload image publication or agent deployment is claimed. | OCI import, image start and model execution remain unverified. |
| Telemetry | Native/rich telemetry contracts, typed clients, history and truthful availability handling are implemented locally. | Exactly 25 Rust telemetry tests passed at `1aeeb310`; adapter/helper tests prove contract behavior and explicit unavailable states, not physical sensor coverage or a 25-metric requirement. | No CI result is claimed for the current integration head. | No deployed telemetry observation is claimed. | NVIDIA sensor accuracy, latency and power history remain unverified. |

The platform rows above are local-only integration evidence. A passing fixture,
helper or unit test is not publication, deployment or hardware evidence.
OrbStack is root-verified as running with the `orbstack` context and an
`aarch64` engine. A sandbox-side Docker permission error is an access boundary
for this worker, not an engine-down claim; container checks must use the
verified OrbStack lane and their results must be recorded separately.

## Current catalog and audit facts

The current recipe authority is `CarstVaartjes/vonk-forge-recipes` at
`10db3c7a73a18319f81103448ddaeada50334bb2`, tag `v1.0.1`. Its schema-2 index
contains all 92 Model documents and all 84 Recipe documents. The direct
relationship set contains 79 Model identities, leaving 13 valid Models with no
direct Recipe reference. That distinction is intentional: a Model can be a
companion, source, or catalog-only candidate and must not be presented as an
executable Recipe.

The Qwen candidate `radixark/qwen3-8-27b-dspark-85ef153b` (`85ef153b`) is one
of the 13 no-direct-Recipe Models. No Recipe package was generated or published
for it, so the platform must not claim an installable Qwen85 workload.

All 84 published Recipes are included in the package/index closure. That is
coverage of the release inventory, not proof that every runtime, upstream pin,
container, serving path or physical target has passed acceptance. The committed
DeepSeek source audit reports `132 current`, `44 advanced`, and `0 errors` for
its audited source set; it names stale published-image refreshes as concrete
blockers. A complete all-84 audit status still needs a per-Recipe evidence
record, including missing release notes, stale or private image pins, missing
runtime/source bundles, and unperformed serving or Spark checks. Do not turn
the package count into an acceptance count.

The approved safety boundary remains active: denied engine arguments and denied
build-network access stay denied unless a source-backed exception is explicitly
approved and tested. Approval questions for mutable-contract-main and the
argument-name blacklist/distributed-bridge changes remain pending. A
mutable-main channel is not a publication authority; its promotion requires
the recorded CI and release approvals. No such approval is inferred from a
branch merge or a helper-only test.

## Checks already available

These are the actual recorded checks carried by the integrated workstreams:

- Connected P9 evidence at `8930f9fb`: 4 OrbStack checks passed and 3 PostgreSQL
  checks passed; owner evidence summary `5168777d`. This is connected local
  acceptance, not CI, publication/deployment, or physical Spark evidence.
- Controller integration evidence before the final Library cutover: the frozen
  suite collected 2,039 cases (`1,935 passed`, `103 skipped`, one macOS-only
  `os.setresgid` failure). This is historical evidence at `571f30bd`, not a
  result for `8930f9fb`.
- Protocol evidence at `571f30bd`: 449 passed and 2 skipped, with 75 focused
  Controller cases and supply-chain verification recorded. This proves the
  tracked wheel boundary, not final workload OCI import.
- Web evidence: build, 251 Vitest tests and 22 Chromium journeys passed at the
  accepted web head, with one optional screenshot capture skip. The exact
  current web CI/deployment identity is the `7eb783d6` / workflow
  `33985962484` / Cloudflare deployment tuple above.
- Recipe package consumer fixtures: 88 passing tests covering changed-only,
  restart, failure, offline and fork behavior. The fixture count is not the
  84-release acceptance count and does not prove publication or hardware.
- Rust/NAS setup evidence: the ranged distribution consumer and 30-case NAS
  setup suite passed in their designated Linux/OrbStack workstreams. They use
  representative bytes and do not prove OCI import, physical LAN performance,
  sensor accuracy or model quality.

## Issue status mapping

The open platform issues map to the current evidence as follows. “Local” means
implemented or exercised in the integration worktree; it does not mean the
issue is closed.

| Issue | Scope | Current evidence and blocker |
|---|---|---|
| [#593](https://github.com/CarstVaartjes/vonk-forge/issues/593) | NAS pre-cache | Local cache identity, download, verification, reuse and protection work exists. Connected NAS sync, deployed storage/retention and an offline-Spark journey remain unverified. |
| [#594](https://github.com/CarstVaartjes/vonk-forge/issues/594) | Reliable progress and ETA | Durable phase/progress projections and restart fixtures exist locally. Browser evolution, cross-client parity and deployed observations remain open; unknown totals must stay unknown. |
| [#595](https://github.com/CarstVaartjes/vonk-forge/issues/595) | Deployment provenance | Exact local source and recipe identities are recorded. Published Controller/agent image digests and deployed provenance are absent from this platform ledger. |
| [#596](https://github.com/CarstVaartjes/vonk-forge/issues/596) | Runtime preflight | Local planning and canonical runtime checks exist. The single schema-2 workload wire/Rust OCI import remains pending, so preflight cannot be called connected or executable. |
| [#597](https://github.com/CarstVaartjes/vonk-forge/issues/597) | Durable/resumable lifecycle | Request keys, checkpoints and receipt projections are locally covered. End-to-end restart/retry with real OCI import and target observation remains open. |
| [#598](https://github.com/CarstVaartjes/vonk-forge/issues/598) | Failure evidence | Bounded typed failure/progress projections exist locally. Deployed sanitized evidence, recovery actions and a physical partial-failure journey remain unverified. |

These issue rows are a status map only. This documentation refresh does not
close, edit or reinterpret the issues.

## Release boundary and next evidence

The next evidence step is to finish and test the single schema-2 workload wire
through Rust OCI import, then run the connected P9 catalog/API and Run/Switch
journeys in the verified OrbStack lane. After that, record compatible
Controller/agent publication and deployment identities, and only then record
physical Spark observations. Recipe publication, web deployment, Controller
deployment and physical acceptance remain separate gates.

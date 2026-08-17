# Vonk Forge Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the installable Vonk Forge control plane and Rust GPU node agent together with the public `vonk-forge-web` recipe catalog, publishing API, and website.

**Architecture:** The local `vonk-forge` repository remains authoritative for nodes, accepted recipes, installations, placements, runs, and route publication. The public `vonk-forge-web` repository owns OAuth publisher identity and immutable public recipe revisions. The products share only versioned JSON Schema and OpenAPI contracts over HTTPS; model weights and community images remain in external artifact registries.

**Tech Stack:** Python 3.12, FastAPI 0.116.1, SQLAlchemy 2.0.42, Alembic 1.16.4, PostgreSQL 18, React 19, TypeScript 7, Vite 8, Rust 2024 edition, Tokio, rustls, Docker Compose, Caddy, LiteLLM, Tailscale, GitHub Actions, Railway.

## Global Constraints

- Local PostgreSQL is authoritative for local recipe/catalog and operational state; Git is not a recipe execution gate.
- Public recipe revisions are immutable canonical JSON documents identified by SHA-256 content hash.
- Recipe documents cannot execute shell, installer, mod, or privileged hook content.
- Community publishers build and host their own public OCI images; Vonk resolves and pins image digests but does not build them.
- The GPU node agent, stable supervisor, and privileged helper are Rust binaries delivered in a signed ARM64 `.deb`.
- GPU node agents initiate outbound mTLS long polling and expose no routine inbound management endpoint.
- Tailscale is the only supported human and inference ingress for the initial local product.
- Caddy statically proxies `/v1/*` to LiteLLM; the controller atomically publishes validated model routes to LiteLLM.
- LiteLLM reaches only the validated GPU node entrypoint on the restricted management LAN; worker ranks use the GPU node fabric.
- The global service never connects to a local database, controller, agent, GPU node, tailnet, or model endpoint.
- Large images and model artifacts never transit Railway or the NAS control plane.
- All behavior changes follow red-green-refactor and preserve existing security tests until an explicitly tested migration removes the old path.

---

## Program order and dependency gates

| Order | Plan | Repository | Depends on | Independently testable result |
|---|---|---|---|---|
| 1 | `2026-08-07-vonk-public-contract-and-web-foundation.md` | `vonk-forge-web` | none | Public schema package, API/web skeleton, PostgreSQL migrations, Compose tests |
| 2 | clean-slate Fleet/Library authority (`2026-08-17-clean-slate-cleanup.md`) | `vonk-forge` | public recipe schema v1 | Local DB authoring, immutable revisions, Library records, API without Git gate or package/deployment compatibility |
| 3 | `2026-08-07-vonk-workload_run-import-and-runtime.md` | `vonk-forge` | local catalog | Exhaustive WorkloadRun import report and typed vLLM/SGLang/llama.cpp compiler |
| 4 | `2026-08-07-vonk-rust-agent-and-debian-package.md` | `vonk-forge` | stable protocol fixtures | Rust agent/helper/supervisor parity and installable signed ARM64 package |
| 5 | `2026-08-07-vonk-install-admission-and-cluster-ux.md` | `vonk-forge` | catalog, importer, Rust operation contract | Disk/memory/topology planning, installation/run lifecycle, cluster UI |
| 6 | `2026-08-07-vonk-global-catalog-and-publishing.md` | `vonk-forge-web` | public foundation | OAuth publishers, drafts, validation worker, immutable publication, website |
| 7 | `2026-08-07-vonk-sync-publishing-and-e2e.md` | both | all prior plans | Local/global OAuth sync, upload/publish, route publication, full staging E2E |

## Cross-repository contract releases

The public repository owns these artifacts:

```text
schemas/recipe/v1.schema.json
schemas/problem/v1.schema.json
schemas/test-report/v1.schema.json
openapi/openapi.json
```

The local repository pins them under:

```text
schemas/global/recipe-v1.schema.json
schemas/global/problem-v1.schema.json
schemas/global/test-report-v1.schema.json
schemas/global/contract.lock.json
```

`contract.lock.json` records the global Git commit and SHA-256 for each file. A local CI job checks canonical bytes and generated client compatibility against a checked-out global contract release. Runtime operation never fetches schema definitions from `main`.

## Release gates

- **Contract gate:** canonical fixtures hash identically in Python, TypeScript, and Rust.
- **Local authority gate:** create/import/install/run operations succeed with the Git remote unavailable.
- **Agent gate:** Python-oracle and Rust-agent protocol/failure fixtures match; physical GPU node install and rollback pass.
- **Catalog gate:** OAuth, ownership, immutable publication, SSRF restrictions, retryable registry failures, backup, and restore pass in staging.
- **Routing gate:** no route exists before all assigned agents return fresh identity and health evidence; withdrawal precedes stop.
- **Remote-access gate:** only the Tailscale service reaches Caddy; ordinary LAN/public clients cannot reach human or inference services.
- **Publication gate:** a locally tested recipe uploads as a private draft and publishes an immutable staging revision without uploading image or weight bytes.

## Completion audit evidence

The program is complete only when the final plan records:

1. commit and release identifiers for both repositories;
2. migration heads and restore-test evidence for both PostgreSQL databases;
3. signed ARM64 `.deb`, checksum, SBOM, provenance, install, upgrade, and rollback results;
4. WorkloadRun fixture coverage by registry/runtime/import disposition;
5. single-node and multi-node physical installation/run/stop/memory-recovery evidence;
6. Tailnet-only UI and OpenAI-compatible inference acceptance;
7. global staging OAuth, draft, validation, publish, download, and local pinning evidence;
8. exact route-generation evidence showing Caddy, LiteLLM, controller, and GPU node responsibilities; and
9. requirement-by-requirement mapping back to `2026-08-06-vonk-forge-catalog-and-installation-design.md`.

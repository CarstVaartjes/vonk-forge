# Vonk Forge Review Remediation Design

## Scope

This design closes the security, consistency, failure-recovery, and delivery gaps found in the joint review of `vonk-forge` and `vonk-forge-web`. The local repository remains authoritative for private recipes, installations, runs, and GPU node state. The global service validates and distributes public immutable recipe revisions.

## Shared runtime contract

Version 1 images are Linux/ARM64, digest-pinned OCI images with the exact label `ai.vonkforge.runtime-interface=v1`. Their configured user is an explicit numeric non-root identity, and the agent adds rootless Podman with a subordinate-UID mapping as a second isolation boundary. Both repositories consume the same vendored policy document and test their validators against it.

## Artifact acquisition

The agent accepts only credential-free HTTPS URLs for generic HTTP artifacts. Each request hop is resolved before use, every DNS answer must be globally routable, curl is pinned to the validated address, redirects are handled one hop at a time, and the redirect count is bounded. Downloads have an enforced byte ceiling derived from `expected_bytes`; Hugging Face downloads share the same bounded transport and a cumulative remaining-byte budget. OCI artifacts remain supported, but only through HTTPS registries whose host passes the same public-address validation, and their staged output is monitored against the declared ceiling. Any failure removes staging data.

The global worker independently verifies that image and artifact observations fit the recipe's declared download, staging, and installed sizes. The GPU node agent repeats enforcement at execution time so a stale or dishonest declaration cannot consume unbounded local storage.

## Atomic orchestration and admission

Install and run acceptance operate inside a caller-owned database transaction. Resource reservations, lifecycle state, the parent job, and every child agent operation commit together or roll back together. Node locks are acquired in stable order. While holding them, run admission re-reads the latest inventory and active host-memory, GPU-memory, endpoint-port, and rendezvous-port reservations before accepting.

Gang start failure withdraws routing and transactionally enqueues a deterministic, idempotent stop operation for every rank. Agent stop uses an idempotent removal command, so cleanup is safe for ranks that never started. Reservations are released only after cleanup succeeds.

## Global catalog safety and scale

Registry HTTP connections use the IP selected by the validated DNS result while retaining the original hostname for TLS SNI and certificate verification. Redirects are separately resolved and pinned.

Anonymous rate limiting is keyed by the trusted client address, never an attacker-selected cookie, and counters live in PostgreSQL so every API replica shares the same limits. Published revision bytes keep a stable ETag, but responses require revalidation because moderation and publisher suspension are mutable. Forking applies those same visibility checks. Search filtering, cursor ordering, and `limit + 1` pagination execute in SQL without an arbitrary first-1000 cap.

## Delivery and verification

Local CI runs the complete control, Rust workspace, and web suites that protect catalog and runtime behavior. The global release workflow builds and signs one immutable GHCR digest, then configures Railway services to deploy that digest. It never performs a second source build for production.

Acceptance requires focused regression tests for every reviewed failure, the full suites in both repositories, schema/contract verification, generated API checks, formatting, and clean worktrees before push.

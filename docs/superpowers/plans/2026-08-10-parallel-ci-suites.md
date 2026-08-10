# Parallel CI Suites Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce pull-request CI latency by running complete control, agent, and web suites concurrently while preserving the required aggregate check.

**Architecture:** Split the serial catalog job into three independent jobs and a stable-name aggregator. Use pinned file-level pytest parallelism only for the control suite; keep agent serial and web test/build together.

**Tech Stack:** GitHub Actions, uv, pytest 9.1.1, pytest-xdist 3.8.0, Vitest/npm.

## Global Constraints

- Keep the required check name `Catalog and service suites`.
- Run every existing complete suite and the admin web production build.
- Pin Python to 3.12 and pytest-xdist to 3.8.0.
- Keep the agent suite serial.
- Preserve downstream release gating through `catalog-runtime`.

---

### Task 1: Parallel Suite Jobs

**Files:**
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_ci_platform_boundaries.py`

**Interfaces:**
- Produces: jobs `control-suite`, `agent-suite`, `web-suite`, and aggregator `catalog-runtime`.
- Consumes: existing `workload-package-evidence.needs.catalog-runtime.result` release gate.

- [ ] **Step 1: Write failing workflow contracts**

Replace the serial-job assertion with checks that each owning job contains its
complete command, control pins `pytest-xdist==3.8.0` with `-n auto --dist
loadfile`, agent contains no xdist flags, and `catalog-runtime` needs all three
jobs while retaining `name: Catalog and service suites`.

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest tests/test_ci_platform_boundaries.py -q`

Expected: failure because the three jobs and aggregator do not exist.

- [ ] **Step 3: Split the workflow**

Add focused checkout/setup steps to control and agent jobs, checkout/npm setup to
the web job, move each complete suite command to its owner, and replace the old
serial `catalog-runtime` body with an always-running dependency-result check.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run --python 3.12 --frozen --with pytest==9.1.1 pytest tests/test_ci_platform_boundaries.py tests/test_container_release_workflow.py -q
uv run --python 3.12 --with pyyaml==6.0.3 python -c 'import pathlib,yaml; yaml.safe_load(pathlib.Path(".github/workflows/ci.yml").read_text())'
git diff --check
```

Expected: all checks pass.

- [ ] **Step 5: Publish and measure**

Commit, open a focused pull request, and compare the Ubuntu job durations with
the 7m05s serial baseline. Merge only if every suite and the preserving
aggregator pass.

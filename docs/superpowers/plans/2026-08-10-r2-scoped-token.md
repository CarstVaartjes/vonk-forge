# R2 Scoped Token Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow least-privilege bucket-scoped R2 tokens to publish APT objects through rclone.

**Architecture:** Configure rclone's Cloudflare remote to skip unauthorized bucket-level checks in every APT publication phase. Enforce complete coverage with a workflow contract test.

**Tech Stack:** GitHub composite actions, rclone S3 provider, pytest.

## Global Constraints

- Keep the R2 token scoped to the development public and state buckets.
- Set `RCLONE_CONFIG_R2_NO_CHECK_BUCKET` to the string `true` in every R2 environment.
- Do not expose or rotate credentials.

---

### Task 1: Configure Scoped R2 Access

**Files:**
- Modify: `.github/actions/agent-apt-publish/action.yml`
- Test: `tests/test_agent_release_workflow.py`

**Interfaces:**
- Consumes: the existing `r2` remote configured through `RCLONE_CONFIG_R2_*` environment variables.
- Produces: `RCLONE_CONFIG_R2_NO_CHECK_BUCKET: "true"` for prepare, state commit, and public publish phases.

- [ ] **Step 1: Write the failing workflow contract**

Assert that the action has exactly three `RCLONE_CONFIG_R2_TYPE: s3` entries,
exactly three `RCLONE_CONFIG_R2_NO_CHECK_BUCKET: "true"` entries, and that the
counts match.

- [ ] **Step 2: Verify RED**

Run: `uv run --project control --frozen pytest tests/test_agent_release_workflow.py -q`

Expected: failure because no scoped-token compatibility setting exists.

- [ ] **Step 3: Add the rclone setting**

Add `RCLONE_CONFIG_R2_NO_CHECK_BUCKET: "true"` beside the provider and type in
the prepare, immutable state commit, and public publish environments.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run --project control --frozen pytest tests/test_agent_release_workflow.py -q
git diff --check
```

Expected: all tests pass.

- [ ] **Step 5: Publish and verify**

Commit, open a focused pull request, merge after required checks pass, and watch
the resulting main development APT workflow until both R2 state and public
publication complete successfully.

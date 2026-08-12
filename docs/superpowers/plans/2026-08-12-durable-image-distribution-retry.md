# Durable Image Distribution Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the development acceptance runner perform one deterministic explicit retry after a terminal image-distribution operation.

**Architecture:** Reuse `Runner.operation`'s existing one-retry boundary and the controller's existing explicit retry endpoint. Supply a UUIDv5 request key only from `image_distributed`; retain every exact distribution-evidence check and fail after a second terminal result.

**Tech Stack:** Python 3.12, pytest, existing in-process HTTP slice server.

## Global Constraints

- A successful initial distribution performs no retry.
- A terminal initial distribution performs exactly one retry with the deterministic `image-distributed:distribution-retry` request key.
- A terminal retry still fails closed and leaves evidence completed only through `image-built`.
- No controller API, schema, operation-authority, digest, byte-count, owner, or node-evidence validation changes.

---

### Task 1: Wire one explicit distribution retry

**Files:**
- Modify: `scripts/run-development-slices`
- Test: `scripts/tests/test_run_development_slices.py`

**Interfaces:**
- Consumes: `Runner.operation(..., retry_request_key: str | None)` and `Runner.request_key(state, purpose)`.
- Produces: one retry key from `request_key("image-distributed", "distribution-retry")` passed only to the image-distribution operation.

- [ ] **Step 1: Write failing tests**

Extend `SliceServer` only as necessary to configure the initial distribution
operation state and reuse its existing retry response. Add tests that:

```python
def test_runner_retries_one_terminal_image_distribution(tmp_path, server):
    server.distribution_operation_state = "failed"
    result, evidence_path = _run(tmp_path, server)
    assert result.returncode == 0, result.stderr
    assert sum(path.endswith("/retry") for _, path, _ in server.requests) == 1
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES


def test_runner_stops_after_one_terminal_distribution_retry(tmp_path, server):
    server.distribution_operation_state = "failed"
    server.retry_operation_state = "failed"
    result, evidence_path = _run(tmp_path, server)
    assert result.returncode == 1
    assert "image distribution operation ended in failed" in result.stderr
    assert json.loads(evidence_path.read_text())["completed_states"] == STATES[:4]
```

Use the test fixture's actual state names and response fields; do not weaken
existing assertions to make the tests fit.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest scripts/tests/test_run_development_slices.py -k 'terminal_image_distribution or terminal_distribution_retry' -q
```

Expected: the recovery test fails because no distribution retry request is made.

- [ ] **Step 3: Implement the minimal wiring**

Change only the distribution `self.operation(...)` call:

```python
operation_id, _owner, result = self.operation(
    response,
    "image distribution",
    expected_nodes=distribution_nodes,
    retry_request_key=self.request_key(
        "image-distributed", "distribution-retry"
    ),
)
```

- [ ] **Step 4: Verify GREEN and surrounding tests**

Run:

```bash
uv run pytest scripts/tests/test_run_development_slices.py -q
uv run ruff check scripts/run-development-slices scripts/tests/test_run_development_slices.py
uv run ruff format --check scripts/run-development-slices scripts/tests/test_run_development_slices.py
git diff --check
```

Expected: all commands pass without warnings.

- [ ] **Step 5: Commit**

```bash
git add scripts/run-development-slices scripts/tests/test_run_development_slices.py
git commit -m "fix: retry terminal image distribution once"
```

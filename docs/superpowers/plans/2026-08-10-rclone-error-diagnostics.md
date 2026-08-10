# Redacted Rclone Error Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make failed R2 operations actionable in GitHub Actions without allowing configured credentials or sensitive URL components into logs.

**Architecture:** Keep subprocess handling inside `RcloneStore`, but make callers label each invocation as `list`, `read`, or `write`. A focused sanitizer retains only the final non-empty error line after replacing configured R2 values, URL user information, and query strings.

**Tech Stack:** Python 3.12, `subprocess`, regular expressions, pytest, GitHub Actions.

## Global Constraints

- Never log command arguments, object payloads, environment contents, or unredacted subprocess output.
- Report the operation and subprocess exit code for every failed invocation.
- Redact configured R2 credentials and endpoint values, URL user information, and URL query strings.
- Preserve a recognizable non-secret provider error when stderr contains one.

---

### Task 1: Redacted Rclone Failures

**Files:**
- Modify: `scripts/agent-apt-state:548`
- Test: `tests/scripts/test_agent_apt_state.py`

**Interfaces:**
- Consumes: `RCLONE_CONFIG_R2_ACCESS_KEY_ID`, `RCLONE_CONFIG_R2_SECRET_ACCESS_KEY`, and `RCLONE_CONFIG_R2_ENDPOINT` from the process environment.
- Produces: `RcloneStore._run(operation: str, arguments: list[str], *, input_raw: bytes | None = None) -> bytes` and `_sanitize_rclone_error(raw: bytes) -> str`.

- [ ] **Step 1: Write failing tests for redaction and empty stderr**

Add tests that monkeypatch `subprocess.run` to return failed `CompletedProcess` values. Supply stderr containing every configured value, a credential-bearing URL with a query string, and `AccessDenied`; assert the `StateError` contains `write failed with exit code 3` and `AccessDenied`, while none of the secrets, endpoint, URL credentials, or query values remain. Add a second test with empty stderr and assert `read failed with exit code 9` remains.

```python
def test_rclone_failure_reports_sanitized_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    state = load_state_module()
    secrets = {
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID": "example-access-key",
        "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": "example-secret-key",
        "RCLONE_CONFIG_R2_ENDPOINT": "https://example-account.r2.cloudflarestorage.com",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    stderr = (
        "request failed for https://url-user:url-password@example.invalid/object"
        "?signature=query-secret: AccessDenied "
        + " ".join(secrets.values())
    ).encode()
    monkeypatch.setattr(
        state.subprocess,
        "run",
        lambda *args, **kwargs: state.subprocess.CompletedProcess(
            args[0], 3, stdout=b"", stderr=stderr
        ),
    )

    with pytest.raises(state.StateError) as raised:
        state.RcloneStore("valid-bucket")._run("write", ["copyto", "source", "target"])

    message = str(raised.value)
    assert "write failed with exit code 3" in message
    assert "AccessDenied" in message
    for value in (*secrets.values(), "url-user", "url-password", "query-secret"):
        assert value not in message


def test_rclone_failure_without_stderr_reports_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    state = load_state_module()
    monkeypatch.setattr(
        state.subprocess,
        "run",
        lambda *args, **kwargs: state.subprocess.CompletedProcess(
            args[0], 9, stdout=b"", stderr=b""
        ),
    )

    with pytest.raises(state.StateError, match="read failed with exit code 9$"):
        state.RcloneStore("valid-bucket")._run("read", ["cat", "target"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run --project control --frozen pytest tests/scripts/test_agent_apt_state.py -q`

Expected: the new tests fail because `_run` does not accept an operation and discards stderr.

- [ ] **Step 3: Implement the minimal sanitizer and operation labels**

Add `_sanitize_rclone_error`, change `_run` to accept an operation, and pass `list`, `read`, and `write` from the three public methods. Decode stderr with replacement, redact non-empty configured values, redact URL user information and query strings, select the final non-empty line, and cap it at 512 characters. Raise `StateError(f"object store {operation} failed with exit code {code}: {detail}")`, omitting the colon and detail when stderr is empty.

- [ ] **Step 4: Run focused and workflow tests**

Run:

```bash
uv run --project control --frozen pytest tests/scripts/test_agent_apt_state.py tests/test_agent_release_workflow.py -q
uv run --project control --frozen ruff check scripts/agent-apt-state tests/scripts/test_agent_apt_state.py
git diff --check
```

Expected: all tests and checks pass.

- [ ] **Step 5: Commit the implementation**

```bash
git add scripts/agent-apt-state tests/scripts/test_agent_apt_state.py docs/superpowers/plans/2026-08-10-rclone-error-diagnostics.md
git commit -S -m "fix(apt): report redacted rclone failures"
```

- [ ] **Step 6: Publish and diagnose the provider response**

Push the branch, open a pull request, wait for required checks, merge it, and rerun failed jobs for Actions run `31380794496`. Use the resulting sanitized operation/provider message to correct only the identified R2 configuration or code defect, then verify the APT run succeeds and the development state and public buckets contain committed objects.

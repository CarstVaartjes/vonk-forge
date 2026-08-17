# Task 7 caller parity fix report

## Scope

Changed only the authorized caller and its test harness:

- `scripts/run-development-slices`
- `scripts/tests/test_run_development_slices.py`

The runner now previews Stop and Uninstall actions, requires an allowed preview
with a nonempty string `plan_digest`, and submits that exact digest with the
existing deterministic request key. Grouped operation verification is unchanged.
Uninstall still does not issue an implicit Stop or delete a catalog record.

## TDD evidence

Tests and fake-authority enforcement were added before changing
`scripts/run-development-slices`.

Initial command attempted:

```text
pytest -q scripts/tests/test_run_development_slices.py -k 'lifecycle_action'
```

It could not start because `pytest` was not on PATH. The repository documents
the frozen `uv` invocation, which was then used without changing dependencies.

RED command:

```text
uv run --frozen pytest -q scripts/tests/test_run_development_slices.py -k 'lifecycle_action'
```

RED result:

```text
4 failed, 2 passed, 48 deselected
```

The four caller cases failed because the former Stop/Uninstall applies lacked
a preview admission and digest; the fake authority rejected the applies with
HTTP 409. The two direct fake-authority cases confirmed that a wrong digest is
rejected.

The first RED attempt also exposed an ignored Python bytecode file inside the
source fixture, which made the unchanged baseline abort before lifecycle
actions. The test harness now makes a per-test copy excluding `__pycache__`, so
the fixture archive matches its pinned source identity. This is limited to the
owned test file.

GREEN command:

```text
uv run --frozen pytest -q scripts/tests/test_run_development_slices.py -k 'lifecycle_action'
```

GREEN result:

```text
6 passed, 48 deselected
```

The focused set proves:

- Stop preview precedes apply and the returned digest is submitted.
- Uninstall preview precedes apply and the returned digest is submitted.
- Blocked Stop and Uninstall previews raise clear `SliceError` messages before apply.
- The fake authority rejects stale or wrong Stop and Uninstall apply digests.

## Final verification

```text
uv run --frozen pytest -q --disable-warnings scripts/tests/test_run_development_slices.py
uv run --with ruff==0.16.1 ruff check scripts/run-development-slices scripts/tests/test_run_development_slices.py
uv run --frozen python -m py_compile scripts/run-development-slices
git diff --check HEAD^ HEAD
```

All commands completed successfully. Ruff was invoked as the exact declared
version (`0.16.1`) because it is not installed in the frozen project environment.

## Commits

- `5efc5fcea9eb7c5dad04472bda7b8909011a4966` — caller and test changes
- Report commit follows this file.

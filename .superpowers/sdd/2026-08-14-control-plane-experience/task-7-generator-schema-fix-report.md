# Task 7 generated-client schema identity fix report

Implementation commit: `9b92bd5` (`fix(control): disambiguate library reason schema`),
based on `c78dcc1`.

## Scope

Changed only the authorized Library contract and its narrow in-memory Library
API/OpenAPI test:

- `control/src/vonk_control/library_contract.py`
- `control/tests/test_library_api.py`

The Library `ProjectionReason` class keeps its existing fields and inherited
strict model behavior, but explicitly emits the unique Pydantic/OpenAPI title
`LibraryProjectionReason`. Fleet continues to emit `ProjectionReason`.

The generated `control/openapi.json`, generated web declarations, and generated
Python client diagnostic outputs were pre-existing dirty controller artifacts.
They were neither edited, staged, restored, nor committed. No generator, live
system, push, or pull request was used.

## TDD evidence

The regression was written before changing `library_contract.py`. It generates
the authenticated admin OpenAPI schema in memory and verifies that:

- Library's reason component title is `LibraryProjectionReason` while Fleet's
  remains `ProjectionReason`.
- Every reachable Library response component with a `reasons` field references
  the unique Library reason component, and each traversed component reference
  resolves.
- Library reason input remains strict: an integer `code` and an unexpected
  field both raise Pydantic `ValidationError`.

RED command:

```text
uv run --project control --frozen pytest control/tests/test_library_api.py::test_library_openapi_reason_schema_has_a_unique_strict_identity -q
```

RED result: `1 failed`. The observed failure was exactly the schema collision:
Library's emitted title was `ProjectionReason`, not `LibraryProjectionReason`.

GREEN command: the same command passed after the one-line Library model title
override (`1 passed`).

## Final verification

```text
uv run --project control --frozen pytest control/tests/test_library_api.py control/tests/test_library_projection.py control/tests/test_operation_api.py -q
uv run --frozen --with ruff==0.16.1 ruff check control/src/vonk_control/library_contract.py control/tests/test_library_api.py
uv run --frozen --with ruff==0.16.1 ruff format --check control/src/vonk_control/library_contract.py control/tests/test_library_api.py
uv run --project control --frozen python -m compileall -q control/src/vonk_control control/tests
git diff --check -- control/src/vonk_control/library_contract.py control/tests/test_library_api.py
```

Results: `69 passed, 18 warnings`; Ruff `0.16.1` lint passed; both owned files
were formatted; `compileall` and the scoped diff check passed. Ruff was supplied
explicitly because the control project's frozen lock currently contains Ruff
`0.13.3`, which the repository's required-version guard correctly rejects.

## Concern

The passing pytest run emitted 18 existing macOS pytest temporary-directory
cleanup warnings (`OSError: Directory not empty`). They did not affect test
results or the owned files.

Report commit follows this file.

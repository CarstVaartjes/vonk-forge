## 2026-08-11 Task 8A partial report

- Scope completed: `control/tests/fixtures/recipes/dev-http-smoke/{recipe.json,expected.json,context/Dockerfile,context/server.py}` and `control/tests/test_development_recipe_fixture.py`.
- Scope deferred: `scripts/run-development-slices` and `scripts/tests/test_run_development_slices.py` remain untouched.

### RED evidence

- Command: `uv run --project control pytest control/tests/test_development_recipe_fixture.py -q`
- Result: `3 failed`
- Failure mode:
  - missing `control/tests/fixtures/recipes/dev-http-smoke/recipe.json`
  - empty generated source bundle because the fixture context did not exist yet

### GREEN evidence

- Command: `uv run --project control pytest control/tests/test_development_recipe_fixture.py -q`
- Result: `3 passed in 0.19s`

- Command: `uv run --project control pytest control/tests/test_development_recipe_fixture.py control/tests/test_recipe_contract.py control/tests/test_source_policy.py control/tests/test_recipe_api.py control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py -q`
- Result: `44 passed in 10.80s`

### Locked fixture values

- Canonical source bundle SHA-256: `7a65752ee1a950b3b358c66ceaf2007d0eb824a7842d0a67a5b1e3726957eb80`
- Canonical recipe SHA-256: `72f8215c7d4f58343a038b04e3abc65b44ab89eea7790b26c6c2e406682b5f43`
- Source archive bytes: `10240`
- Expanded source bytes: `2469`

### Interface notes

- The fixture uses the existing production paths only:
  - `vonk_control.source_bundles.generate_source_bundle`
  - `vonk_control.recipe_contract.validate_recipe`
  - `vonk_control.recipe_contract.recipe_content_sha256`
  - `vonk_control.source_policy.enforce_build_source_policy`
- No production code changed and no synthetic-only branch was added.
- The Dockerfile stays bounded and deterministic:
  - pinned base image index digest `python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7`
  - no `RUN`, package-manager install, or network-download instructions
  - final explicit numeric non-root user `10001:10001`
- Multi-architecture note: verified on 2026-08-11 with `docker buildx imagetools inspect python:3.12.11-slim-bookworm`, which showed the pinned index includes both `linux/amd64` and `linux/arm64/v8` manifests.

## 2026-08-11 Task 8B runner report

- Added `scripts/run-development-slices` and its fake-service acceptance suite.
- Corrected the runner boundary to read the existing public fleet and endpoint
  evidence APIs. Lifecycle mutations still use only catalog and recipe APIs.
- Kept control administration and inference authentication separate with two
  private token files.

### RED evidence

- Command: `uv run pytest scripts/tests/test_run_development_slices.py -q`
- Result: `17 failed`
- Failure mode: the acceptance runner did not exist.

### GREEN evidence

- Command: `uv run pytest scripts/tests/test_run_development_slices.py -q`
- Result: `17 passed in 10.96s`
- Coverage includes the exact 12-state sequence, resume after every completed
  state, failed-gate refusal, authorization redaction, separate credentials,
  private regular-file token checks, and atomic mode-`0600` evidence.

- Command: `uv run --project control pytest control/tests/test_development_recipe_fixture.py control/tests/test_recipe_api.py control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py -q`
- Result: `26 passed in 10.24s`

- Command: `.venv/bin/python -m py_compile scripts/run-development-slices scripts/tests/test_run_development_slices.py`
- Result: passed.

- Commands: `uv lock --project control --check` and `uv lock --check`
- Result: both lock graphs resolve successfully. The project-specific lock now
  records the already-declared PyYAML development dependency required by the
  development Compose validation suite.

### Independent review resolution

- Review found one P1: the runner initially allowed bearer-token requests to
  arbitrary plain-HTTP hosts.
- Added a failing regression for non-loopback HTTP, then restricted HTTP to
  explicit loopback IP addresses; all other control and inference origins must
  use HTTPS.
- Final runner suite: `19 passed in 11.54s` under `uv run --frozen`.
- Controller self-review also added exact canonical recipe/source matching for
  pre-existing same-slug recipes before the independent review.

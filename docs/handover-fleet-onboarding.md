# Fleet onboarding handover

Date: 20260818
Status: Open

## Summary

The control-plane Fleet **Add Spark** flow is not currently usable end to end. The
failure first appears as:

```text
Control API returned 422: [object Object]
```

This is a control-web/control-API contract problem. It is not a Spark host or
Spark agent deployment problem.

## Confirmed issues

### 1. Invalid grant lifetime from the UI

`control/web/src/pages/fleet.tsx` calls:

```ts
api.createEnrollmentGrant(900)
```

The API model in `control/src/vonk_control/agent_api.py` accepts `ttl_seconds`
from 1 through 600 only. The request therefore receives HTTP 422. Use 600 or
less, preferably a named shared constant so the UI and API cannot drift again.

### 2. Validation errors are hidden by the UI

The generated API error contains a structured FastAPI validation document. The
web client currently converts the nested `detail` value with `String(...)`,
which produces `[object Object]` instead of the validation message. Preserve
and format structured validation details in the displayed error.

### 3. Generated bootstrap command is incomplete

The UI currently renders only:

```text
vonk-agent bootstrap --token <token>
```

`vonk-agent bootstrap` requires these additional arguments:

```text
--controller-endpoint <origin> \
--enrollment-endpoint <origin> \
--ca-fingerprint <sha256> \
--config <absolute-path> \
--state-root <absolute-path> \
--ca-path <absolute-path>
```

The grant response already includes controller endpoint, enrollment endpoint,
and CA fingerprint. The deployment-specific absolute paths still need to be
chosen and documented for the Spark installation layout. The command must be
shell-safe and must not expose the token in an avoidable shell-history path.

### 4. Grant response currently contains a placeholder CA fingerprint

The API response currently sets:

```python
ca_fingerprint="0" * 64
```

The agent verifies the supplied fingerprint against the local controller CA and
fails closed on a mismatch. The grant must return the fingerprint of the actual
CA used by the deployed control plane. Do not solve this by weakening agent
verification.

## Required implementation sequence

1. Add a shared maximum-grant-TTL constant or use the API contract value and
   change the UI request from 900 to a valid value.
2. Add a regression test proving the Add Spark action requests a valid TTL.
3. Format FastAPI validation errors into bounded, human-readable text.
4. Resolve the canonical Spark paths for config, state, CA, and one-use token;
   add them to the generated command without leaking secrets.
5. Source the real CA fingerprint from the deployed control-plane trust
   configuration and return it in the grant response.
6. Regenerate/update the OpenAPI TypeScript contract if the response shape or
   request shape changes.
7. Run backend enrollment tests, frontend tests, TypeScript checking, and the
   production web build.
8. Deploy the control plane/NAS application and verify the complete flow:
   create grant, run bootstrap on a Spark, approve enrollment, repeat bootstrap,
   and confirm the authenticated agent becomes live.

## Deployment boundary

No Spark redeployment is required for the already-pushed CI changes. A Spark
agent package rollout is required only if the implementation changes the
agent-side bootstrap protocol or agent binary. Control-plane/web-only fixes
require a control-plane/NAS redeployment, followed by a browser hard refresh.

## Evidence and verification limits

- Commit `44aa7fd4` is present on `main` and `origin/main`.
- GitHub Actions run `32186260124` passed image acceptance and publication.
- The frontend test/build commands were not runnable in the local checkout
  because `control/web/node_modules` is absent.
- The backend test environment could not install pinned
  `psycopg-binary==3.2.9` for the local macOS platform.

## Acceptance criteria for closure

- Add Spark creates a grant without HTTP 422.
- Errors show actionable validation text, never `[object Object]`.
- The displayed command contains every required bootstrap argument and uses the
  deployed CA fingerprint.
- A fresh grant completes Spark bootstrap and pending-enrollment approval.
- Re-running the same bootstrap request is idempotent according to the existing
  enrollment contract.
- The changed control-plane revision is deployed and the local `main` checkout
  has no unpushed or untracked work.

# Control web fixture acceptance

These journeys use the local fixture-backed server only. They do not connect
to Tailscale, NAS, Sparks, or the deployed control plane.

From the repository root:

```bash
cd control/web
npm ci
npm test -- --run
npm run build
npx playwright test e2e/fleet-library.spec.ts --project=chromium
```

The fixture server is started by the Playwright configuration. To inspect a
journey interactively, use the same fixture command/configuration and keep the
browser origin local. Never replace it with the private production URL.

Acceptance should cover Fleet live update/reconnect, retained history and node
detail, Library URL/back history, one model with many recipes, unlinked recipes,
complete two-node recommendations, visual recipe details, action
preview/cancel/apply/progress/partial failure/retry, Advanced JSON upload with
last-valid visualization, mobile drill-down, keyboard focus return, and
empty/error recovery. Run at 360, 768, 1280, and 1920 CSS pixels; assert no
document overflow, visible focus, status announcements, and no console
warnings/errors.

Known local limitations are acceptable only when documented: Docker-gated
PostgreSQL tests and Linux-only `/proc`/credential behavior do not run on a
Darwin laptop. Do not work around those skips by touching live infrastructure.

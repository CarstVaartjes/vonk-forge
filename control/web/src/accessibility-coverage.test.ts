import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { appRoutes } from "./components/ui/app-shell";

// Every routed page must be opened by an e2e journey that also runs the axe check.
test("every route in the app shell has an axe check in the admin e2e", () => {
  const journeys = readFileSync(resolve(import.meta.dirname, "../e2e/admin.spec.ts"), "utf8")
    .split(/\n(?=test\()/)
    .filter((journey) => journey.includes("expectNoSeriousAccessibilityViolations(page)"));
  const unchecked = appRoutes.filter(
    (route) => !journeys.some((journey) => journey.includes(`goto("/${route}`)),
  );
  expect(unchecked, `routes without an axe check in e2e/admin.spec.ts`).toEqual([]);
});

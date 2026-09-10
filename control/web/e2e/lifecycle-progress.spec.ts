import {expect, test} from "@playwright/test";

test("Fleet loads a numbered Profile through the shared operator routes", async ({page}) => {
  const now = new Date().toISOString();
  const nodeId = `spk_${"1".repeat(32)}`;
  const profile = {schema_version: 2, id: "11111111-1111-4111-8111-111111111111", number: 1, revision: 1, name: "Studio", description: "Qwen Chat on Spark One", installation_policy: "keep-cached", labels: {}, favorite: true, assignments: [], fleet: [{selector: nodeId, display_name: "Spark One", state: "idle"}], status: "ready", loaded_revision: null, cache_summary: {}, warnings: [], next_actions: [], profile_digest: "a".repeat(64), created_by: "admin", created_at: now, updated_at: now};
  const application = {schema_version: 2, id: "22222222-2222-4222-8222-222222222222", profile_id: profile.id, profile_digest: profile.profile_digest, plan_digest: "b".repeat(64), attempt: 1, retry_of_application_id: null, state: "running", current_step: 0, total_steps: 1, current_operation_id: null, status_reason: null, progress: {attempt: 1, retry_of_application_id: null, completed_steps: 0, total_steps: 1, child_progress: {phase: "start", node_ids: [nodeId], bytes: 42, total_bytes: 100}}, result: null, created_at: now, updated_at: now};
  await page.route("**/api/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: now, authority_revision: "a".repeat(64), nodes: [{id: nodeId, display_name: "Spark One", hostname: "spark-one.fixture.invalid", lifecycle: "managed", labels: {}, connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: now, last_seen_age_seconds: 0}, inventory: null, telemetry: null, installed: [], loaded: [], reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0}, warnings: []}]}}));
  await page.route("**/api/profile", route => route.fulfill({json: {schema_version: 2, generated_at: now, profiles: [profile]}}));
  await page.route("**/api/profile/1/preview", route => route.fulfill({json: {schema_version: 2, profile_id: profile.id, profile_name: profile.name, profile_digest: profile.profile_digest, generated_at: now, allowed: true, scope: {node_ids: [nodeId], idle_node_ids: [nodeId]}, summary: {already_correct: 0, placements: 0, builds: 0, distributions: 0, installs: 0, starts: 0, stops: 0, uninstalls: 0, blockers: 0}, assignments: [], preparations: [], steps: [{index: 0, kind: "start", label: "Load Studio", node_ids: [nodeId]}], reasons: [], plan_digest: "c".repeat(64)}}));
  await page.route("**/api/profile/1/load", route => route.fulfill({status: 202, json: application}));
  await page.route("**/api/profile/1/progress", route => route.fulfill({json: application}));

  await page.goto("/fleet");
  await page.getByRole("button", {name: "Load profile", exact: true}).click();
  await expect(page.getByRole("region", {name: "Profile load progress"})).toBeVisible();
  await expect(page.getByText("start")).toBeVisible();
});

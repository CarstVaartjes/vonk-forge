import {AuthenticationRequired} from "../auth";
import {ApiClient} from "./client";

afterEach(() => {
  document.cookie = "vonk_csrf=; Max-Age=0; path=/";
  document.cookie = "other_cookie=; Max-Age=0; path=/";
  document.cookie = "third_cookie=; Max-Age=0; path=/";
  vi.unstubAllGlobals();
});

it("keeps visual Fleet snapshots separate from reconciliation evidence", async () => {
  const captured: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    const request = input as Request;
    captured.push(request);
    const pathname = new URL(request.url).pathname;
    const body = pathname === "/api/v1/fleet"
      ? {
        schema_version: 1,
        event_cursor: 7,
        generated_at: "2026-08-15T12:00:00Z",
        repository_commit: "a".repeat(40),
        nodes: [],
      }
      : {commit: "a".repeat(40), evidence_digest: "e".repeat(64), nodes: []};
    return new Response(JSON.stringify(body), {
      headers: {"Content-Type": "application/json"},
      status: 200,
    });
  });
  const api = new ApiClient();

  const visual = await api.visualFleet();
  const evidence = await api.fleetEvidence();
  const statuses = await api.nodeStatuses();

  expect(visual).toEqual({
    schema_version: 1,
    event_cursor: 7,
    generated_at: "2026-08-15T12:00:00Z",
    repository_commit: "a".repeat(40),
    nodes: [],
  });
  expect(evidence.evidence_digest).toBe("e".repeat(64));
  expect(statuses.evidence_digest).toBe("e".repeat(64));
  expect(captured.map(request => new URL(request.url).pathname)).toEqual([
    "/api/v1/fleet",
    "/api/v1/nodes/status",
    "/api/v1/nodes/status",
  ]);
  expect(captured.every(request => request.credentials === "same-origin")).toBe(true);
});

it("uses distinct digest-bound Library action operations", async () => {
  // Break caught: the visual Library falls back to legacy evidence routes,
  // action apply bypasses its server preview digest, or one selected owner is
  // replaced by a browser-invented group.
  const requests: Request[] = [];
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/api/v1/library") return new Response(JSON.stringify({schema_version: 1, generated_at: "2026-08-15T12:00:00Z", freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20}, models: [], unlinked_recipes: [], next_cursor: null}), {status: 200});
    if (path === "/api/v1/library/recipes/recipe%2Fone") return new Response(JSON.stringify({schema_version: 1, generated_at: "2026-08-15T12:00:00Z", recipe: {recipe_id: "recipe/one", slug: "one", title: "One", description: "", source_kind: "local"}, selected_revision: null, visual_recipe: null, profiles: [], operational_state: {builds: [], mappings: [], installations: [], runs: []}, placement: [], reasons: []}), {status: 200});
    if (path.startsWith("/api/v1/jobs/")) return new Response(JSON.stringify({id: "job-1", kind: "recipe.install", state: "running", base_commit: "a".repeat(40), current_attempt: 1, operations: [], operation_total: 0, targets: [], target_total: 0, progress: {completed: 0, failed: 0, running: 1, total: 1}}), {status: 200});
    return new Response(JSON.stringify({
      id: "operation-1", kind: "recipe.install", owner_id: "owner-1", state: "queued",
      plan_digest: "plan-1", nodes: ["node-a", "node-b"], result: null,
      allowed: true, blockers: [], warnings: [],
    }), {status: request.method === "GET" ? 200 : request.url.includes("preview") ? 200 : 202});
  });
  const api = new ApiClient();

  await api.librarySnapshot("cursor-1");
  await api.libraryRecipe("recipe/one");
  await api.previewLibraryMapping({recipe_revision_id: "revision-1", profile_name: "pair", node_ids: ["node-a", "node-b"], parameters: {tensor: 2}});
  await api.applyLibraryMapping({recipe_revision_id: "revision-1", profile_name: "pair", node_ids: ["node-a", "node-b"], parameters: {tensor: 2}, placement_digest: "map-plan"});
  await api.previewLibraryInstall({recipe_build_id: "build-1", mapping_id: "mapping-1"});
  await api.applyLibraryInstall({recipe_build_id: "build-1", mapping_id: "mapping-1", plan_digest: "install-plan"});
  await api.previewLibraryLoad({installation_id: "installation-1"});
  await api.applyLibraryLoad({installation_id: "installation-1", alias: "chat", plan_digest: "load-plan"});
  await api.previewLibraryStop("run-1");
  await api.applyLibraryStop("run-1", "stop-plan");
  await api.previewLibraryUninstall("installation-1");
  await api.applyLibraryUninstall("installation-1", "remove-plan");
  await api.libraryOperation("operation-1");
  await api.retryLibraryOperation("operation-1");
  await api.libraryRunStatus("run-1");
  await api.libraryJobProgress("job-1");

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/api/v1/library"],
    ["GET", "/api/v1/library/recipes/recipe%2Fone"],
    ["POST", "/api/v1/recipes/mapping-plans/preview"],
    ["POST", "/api/v1/recipes/mappings"],
    ["POST", "/api/v1/recipes/install-plans/preview"],
    ["POST", "/api/v1/recipes/installations"],
    ["POST", "/api/v1/recipes/run-plans/preview"],
    ["POST", "/api/v1/recipes/runs"],
    ["POST", "/api/v1/recipes/stop-plans/preview"],
    ["POST", "/api/v1/recipes/runs/run-1/stop"],
    ["POST", "/api/v1/recipes/uninstall-plans/preview"],
    ["POST", "/api/v1/recipes/installations/installation-1/uninstall"],
    ["GET", "/api/v1/recipes/operations/operation-1"],
    ["POST", "/api/v1/recipes/operations/operation-1/retry"],
    ["GET", "/api/v1/recipes/runs/run-1"],
    ["GET", "/api/v1/jobs/job-1"],
  ]);
  expect(Object.fromEntries(new URL(requests[0].url).searchParams)).toEqual({cursor: "cursor-1", limit: "100"});
  expect(await requests[3].clone().json()).toEqual({recipe_revision_id: "revision-1", profile_name: "pair", node_ids: ["node-a", "node-b"], parameters: {tensor: 2}, placement_digest: "map-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[5].clone().json()).toEqual({recipe_build_id: "build-1", mapping_id: "mapping-1", plan_digest: "install-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[7].clone().json()).toEqual({installation_id: "installation-1", alias: "chat", plan_digest: "load-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[9].clone().json()).toEqual({plan_digest: "stop-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[11].clone().json()).toEqual({plan_digest: "remove-plan", request_key: "00000000-0000-4000-8000-000000000001"});
});

it("requests bounded node telemetry history through the generated operation", async () => {
  let captured: Request | undefined;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    captured = input as Request;
    return new Response(JSON.stringify({
      schema_version: 1,
      node_id: "spk_0123456789abcdef0123456789abcdef",
      start: "2026-08-15T11:00:00.000Z",
      end: "2026-08-15T12:00:00.000Z",
      maximum_points: 360,
      points: [],
    }), {headers: {"Content-Type": "application/json"}, status: 200});
  });

  await new ApiClient().nodeTelemetryHistory(
    "spk_0123456789abcdef0123456789abcdef",
    "2026-08-15T11:00:00.000Z",
    "2026-08-15T12:00:00.000Z",
    360,
  );

  const url = new URL(captured!.url);
  expect(url.pathname).toBe("/api/v1/nodes/spk_0123456789abcdef0123456789abcdef/telemetry");
  expect(Object.fromEntries(url.searchParams)).toEqual({
    end: "2026-08-15T12:00:00.000Z",
    maximum_points: "360",
    start: "2026-08-15T11:00:00.000Z",
  });
});

it("uses exact browser-auth documents and the CSRF cookie for server logout", async () => {
  // Break caught: browser login drifts from the closed API document, or logout
  // omits the double-submit CSRF value while claiming to revoke the session.
  document.cookie = "vonk_csrf=synthetic-csrf-value; path=/";
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/api/v1/auth/logout") return new Response(null, {status: 204});
    return new Response(JSON.stringify({subject: "admin", role: "administrator", expires_at: "2026-08-13T21:30:00Z"}), {headers: {"Content-Type": "application/json"}, status: 200});
  });
  const api = new ApiClient();

  expect(await api.session()).toEqual({subject: "admin", role: "administrator", expires_at: "2026-08-13T21:30:00Z"});
  await api.login("admin", "synthetic-test-password");
  await api.logout();

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/api/v1/auth/session"],
    ["POST", "/api/v1/auth/login"],
    ["POST", "/api/v1/auth/logout"],
  ]);
  expect(await requests[1].clone().json()).toEqual({subject: "admin", password: "synthetic-test-password"});
  expect(requests[2].headers.get("X-CSRF-Token")).toBe("synthetic-csrf-value");
  expect(requests.every(request => request.credentials === "same-origin")).toBe(true);
});

it("throws and emits one centralized authentication signal for an API 401", async () => {
  // Break caught: an expired browser session becomes a page-local error rather
  // than a single, in-memory signal that can remove the full control shell.
  vi.stubGlobal("fetch", async () => new Response(JSON.stringify({detail: "authentication failed"}), {
    headers: {"Content-Type": "application/json"}, status: 401,
  }));
  const api = new ApiClient();
  let signals = 0;
  api.onAuthenticationRequired(() => { signals += 1; });

  await expect(api.visualFleet()).rejects.toBeInstanceOf(AuthenticationRequired);
  expect(signals).toBe(1);
});

it("emits one authentication-required callback for a generated revoke 401", async () => {
  // Break caught: generated response middleware and revoke's local response
  // handling each notify the shell for the same expired-session response.
  vi.stubGlobal("fetch", async () => new Response(JSON.stringify({detail: "authentication failed"}), {
    headers: {"Content-Type": "application/json"}, status: 401,
  }));
  const api = new ApiClient();
  let signals = 0;
  api.onAuthenticationRequired(() => { signals += 1; });

  await expect(api.revokeAgentNode("spk_0123456789abcdef0123456789abcdef")).rejects.toBeInstanceOf(AuthenticationRequired);
  expect(signals).toBe(1);
});

it("adds the session CSRF token to generated enrollment mutations", async () => {
  document.cookie = "vonk_csrf=csrf-value; path=/";
  let captured: Request | undefined;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    captured = input as Request;
    return new Response(JSON.stringify({
      expires_at: "2026-08-05T10:15:00Z",
      id: "grant-001",
      node_id: "spk_0123456789abcdef0123456789abcdef",
      purpose: "new-node",
      token: "g".repeat(48),
    }), {headers: {"Content-Type": "application/json"}, status: 201});
  });

  await new ApiClient().createEnrollmentGrant("spk_0123456789abcdef0123456789abcdef", 300);

  expect(captured!.method).toBe("POST");
  expect(captured!.headers.get("X-CSRF-Token")).toBe("csrf-value");
  expect(captured!.credentials).toBe("same-origin");
});

it("surfaces bounded stable API guidance for local catalog workflows", async () => {
  vi.stubGlobal("fetch", async () => new Response(JSON.stringify({
    code: "global.revision_changed",
    detail: "The immutable revision no longer matches; review it again.",
    request_id: "10000000-0000-4000-8000-000000000001",
  }), {headers: {"Content-Type": "application/problem+json"}, status: 409}));

  await expect(new ApiClient().previewGlobalRecipe(
    `vonk://catalog/vonk/qwen@sha256:${"a".repeat(64)}`,
  )).rejects.toThrow("global.revision_changed: The immutable revision no longer matches; review it again.");
});

it.each(["nonce=", "nonce==", "nonce=middle=="]) (
  "preserves the complete padded CSRF cookie value %s among multiple cookies",
  async csrfValue => {
    // Break caught: splitting every '=' silently truncates padded CSRF tokens.
    document.cookie = "other_cookie=other-value; path=/";
    document.cookie = `vonk_csrf=${csrfValue}; path=/`;
    document.cookie = "third_cookie=third-value; path=/";
    let captured: Request | undefined;
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      captured = input as Request;
      return new Response(JSON.stringify({
        expires_at: "2026-08-05T10:15:00Z",
        id: "grant-001",
        node_id: "spk_0123456789abcdef0123456789abcdef",
        purpose: "new-node",
        token: "g".repeat(48),
      }), {headers: {"Content-Type": "application/json"}, status: 201});
    });

    await new ApiClient().createEnrollmentGrant(
      "spk_0123456789abcdef0123456789abcdef",
      300,
    );

    expect(captured!.headers.get("X-CSRF-Token")).toBe(csrfValue);
  },
);

it("uses one exact API contract for update plan, apply, status, and administrator resume", async () => {
  // Break caught: the browser update workflow drifts from CLI routes or omits
  // the exact server plan digest on the only fan-out mutation.
  document.cookie = "vonk_csrf=csrf-value; path=/";
  const digest = `sha256:${"c".repeat(64)}`;
  const targetName = `platform/releases/2.0.0/${"7".repeat(64)}.json`;
  const target = {
    platform_version: "2.0.0",
    release: targetName,
    release_digest: `sha256:${"7".repeat(64)}`,
    target_sha256: "7".repeat(64),
  };
  const rolloutId = "11111111-1111-4111-8111-111111111111";
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/api/v1/updates/skew") return new Response(JSON.stringify({prompt_required: false, target}), {status: 200});
    if (path === "/api/v1/updates/plan") return new Response(JSON.stringify({plan_digest: digest, target}), {status: 200});
    return new Response(JSON.stringify({id: rolloutId, plan_digest: digest, state: "planned"}), {status: request.method === "POST" ? 202 : 200});
  });
  const api = new ApiClient();

  await api.updateSkew();
  await api.planUpdate(targetName);
  await api.applyUpdate(digest);
  await api.updateStatus(rolloutId);
  await api.approveUpdateResume(rolloutId);

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/api/v1/updates/skew"],
    ["POST", "/api/v1/updates/plan"],
    ["POST", "/api/v1/updates"],
    ["GET", `/api/v1/updates/${rolloutId}`],
    ["POST", `/api/v1/updates/${rolloutId}/approve-resume`],
  ]);
  expect(await requests[1].clone().json()).toEqual({release: targetName});
  expect(await requests[2].clone().json()).toEqual({plan_digest: digest});
  expect(requests[1].headers.get("X-CSRF-Token")).toBe("csrf-value");
  expect(requests[2].headers.get("X-CSRF-Token")).toBe("csrf-value");
  expect(requests[4].headers.get("X-CSRF-Token")).toBe("csrf-value");
});

it("routes package pages through the generated package API, including GC", async () => {
  const candidateId = "a".repeat(64);
  const digest = `sha256:${"b".repeat(64)}`;
  const rolloutId = "11111111-1111-4111-8111-111111111111";
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/api/v1/packages/families") return new Response(JSON.stringify({families: [{id: "llm-runtime", channels: ["stable"], promotion_mode: "admin"}], total: 1}), {status: 200});
    if (path === "/api/v1/packages/candidates" ) return new Response(JSON.stringify({candidates: [{id: candidateId, family_id: "llm-runtime", release_key: "stable/v1", upstream_version: "1.0.0", state: "eligible", reason_code: null}], total: 1}), {status: 200});
    if (path === `/api/v1/packages/candidates/${candidateId}`) return new Response(JSON.stringify({id: candidateId, family_id: "llm-runtime", release_key: "stable/v1", upstream_version: "1.0.0", state: "eligible", reason_code: null, release: {lock_digest: digest, components: [], dependencies: [], provenance: []}}), {status: 200});
    if (path === `/api/v1/packages/candidates/${candidateId}/promotion-preview`) return new Response(JSON.stringify({digest, state: "ready"}), {status: 200});
    if (path === `/api/v1/packages/candidates/${candidateId}/promote`) return new Response(JSON.stringify({release_digest: digest}), {status: 202});
    if (path === "/api/v1/deployments") return new Response(JSON.stringify({deployments: [{id: "chat", family_id: "llm-runtime", release_digest: digest, previous_release_digest: null, state: "active"}], total: 1}), {status: 200});
    if (path === "/api/v1/deployments/chat/rollout-preview") return new Response(JSON.stringify({digest, state: "ready", batches: [], offline_pending: [], download_bytes: 3, storage_bytes: 4}), {status: 200});
    if (path === "/api/v1/deployments/chat/rollouts") return new Response(JSON.stringify({id: rolloutId, plan_digest: digest}), {status: 202});
    if (path === `/api/v1/deployments/chat/rollouts/${rolloutId}`) return new Response(JSON.stringify({id: rolloutId, state: "running", nodes: []}), {status: 200});
    if (path === "/api/v1/packages/inventory") return new Response(JSON.stringify({nodes: [], total: 0}), {status: 200});
    if (path === "/api/v1/packages/gc-preview") return new Response(JSON.stringify({digest, state: "ready", reclaim_bytes: 3}), {status: 200});
    if (path === "/api/v1/packages/gc") return new Response(JSON.stringify({id: rolloutId, plan_digest: digest, state: "accepted", progress: {completed: 0, failed: 0, running: 0, total: 0}}), {status: 202});
    return new Response(JSON.stringify({detail: "unexpected route"}), {status: 404});
  });

  const api = new ApiClient();
  expect((await api.packageFamilies())[0].channels).toEqual(["stable"]);
  expect((await api.packageCandidates())[0].id).toBe(candidateId);
  expect((await api.packageCandidate(candidateId)).lock?.digest).toBe(digest);
  expect((await api.previewPackagePromotion(candidateId)).digest).toBe(digest);
  expect((await api.promotePackage(candidateId, digest)).release_digest).toBe(digest);
  expect((await api.deployments())[0].id).toBe("chat");
  expect((await api.previewPackageRollout("chat")).download_remaining_bytes).toBe(3);
  expect((await api.startPackageRollout("chat", digest)).id).toBe(rolloutId);
  expect((await api.packageRollout("chat", rolloutId)).state).toBe("running");
  expect((await api.packageInventory()).total).toBe(0);
  expect((await api.previewPackageGc()).reclaim_bytes).toBe(3);
  expect((await api.applyPackageGc(digest)).state).toBe("accepted");
  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/api/v1/packages/families"], ["GET", "/api/v1/packages/candidates"],
    ["GET", `/api/v1/packages/candidates/${candidateId}`], ["POST", `/api/v1/packages/candidates/${candidateId}/promotion-preview`],
    ["POST", `/api/v1/packages/candidates/${candidateId}/promote`], ["GET", "/api/v1/deployments"],
    ["POST", "/api/v1/deployments/chat/rollout-preview"], ["POST", "/api/v1/deployments/chat/rollouts"],
    ["GET", `/api/v1/deployments/chat/rollouts/${rolloutId}`], ["GET", "/api/v1/packages/inventory"],
    ["POST", "/api/v1/packages/gc-preview"], ["POST", "/api/v1/packages/gc"],
  ]);
});

it.each(["skew", "plan"])(
  "rejects an update %s whose release digest is not bound to its target bytes",
  async operation => {
    const targetSha = "7".repeat(64);
    const target = {
      platform_version: "2.0.0",
      release: `platform/releases/2.0.0/${targetSha}.json`,
      release_digest: `sha256:${"8".repeat(64)}`,
      target_sha256: targetSha,
    };
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({
      plan_digest: `sha256:${"c".repeat(64)}`,
      prompt_required: false,
      target,
    }), {status: 200}));

    const api = new ApiClient();
    const request = operation === "skew"
      ? api.updateSkew()
      : api.planUpdate(target.release);
    await expect(request).rejects.toThrow("update target identity is invalid");
  },
);

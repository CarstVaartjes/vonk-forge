import {AuthenticationRequired} from "../auth";
import type {AgentRepairManifest, AgentUpgradePlan, RunSwitchPreviewRequest} from "./types";
import {ApiClient} from "./client";

const REPAIR_NODE = `spk_${"a".repeat(32)}`;
const REPAIR_AUTHORITY = "1".repeat(64);
const REPAIR_PACKAGE_SHA = "2".repeat(64);
const REPAIR_MANIFEST: AgentRepairManifest = {
  schema_version: 2,
  kind: "agent-upgrade-repair",
  node_id: REPAIR_NODE,
  authority_sha256: REPAIR_AUTHORITY,
  package: {
    architecture: "linux-arm64",
    package_bytes: 6_000_000,
    package_sha256: REPAIR_PACKAGE_SHA,
    package_signature: "8".repeat(128),
    package_url: `https://install.vonkforge.ai/repair-capsules/${REPAIR_NODE}/${REPAIR_AUTHORITY}/${REPAIR_PACKAGE_SHA}/vonk-forge-agent.deb`,
    package_version: "0.1.0~dev.382+gd1cef9c7d1ce",
    schema_version: 1,
    target_binary_digest: "a".repeat(64),
    target_build_digest: `sha256:${"9".repeat(64)}`,
  },
};

async function apiErrorMessage(detail: unknown): Promise<string> {
  vi.stubGlobal("fetch", async () => new Response(JSON.stringify({detail}), {
    headers: {"Content-Type": "application/json"},
    status: 422,
  }));
  const error = await new ApiClient().visualFleet().then(
    () => new Error("expected the API request to fail"),
    reason => reason as Error,
  );
  expect(error).toBeInstanceOf(Error);
  return error.message;
}

afterEach(() => {
  document.cookie = "vonk_csrf=; Max-Age=0; path=/";
  document.cookie = "other_cookie=; Max-Age=0; path=/";
  document.cookie = "third_cookie=; Max-Age=0; path=/";
  vi.unstubAllGlobals();
});

it("formats FastAPI validation details with dotted locations and messages", async () => {
  const message = await apiErrorMessage([
    {type: "less_than_equal", loc: ["body", "ttl_seconds"], msg: "Input should be less than or equal to 900", input: 901},
    {type: "string_type", loc: ["body", "name"], msg: "Input should be a valid string"},
  ]);

  expect(message).toContain("body.ttl_seconds: Input should be less than or equal to 900");
  expect(message).toContain("body.name: Input should be a valid string");
  expect(message).not.toContain("[object Object]");
  expect(message.length).toBeLessThanOrEqual("Control API returned 422: ".length + 256);
});

it("JSON-formats bounded nested objects without stringifying them as object tags", async () => {
  const message = await apiErrorMessage({reason: {code: "invalid", fields: ["ttl_seconds"]}});

  expect(message).toContain('{"reason":{"code":"invalid","fields":["ttl_seconds"]}}');
  expect(message).not.toContain("[object Object]");
  expect(message.length).toBeLessThanOrEqual("Control API returned 422: ".length + 256);
});

it("preserves plain string API details", async () => {
  const message = await apiErrorMessage("The requested fleet is not ready");

  expect(message).toContain("The requested fleet is not ready");
  expect(message).not.toContain("[object Object]");
  expect(message.length).toBeLessThanOrEqual("Control API returned 422: ".length + 256);
});

it("omits secret-like input fields from bounded object details", async () => {
  const message = await apiErrorMessage({
    reason: "invalid request",
    input: {password: "do-not-leak", token: "also-secret"},
    nested: {input: "nested-secret", safe: true},
    padding: "x".repeat(400),
  });

  expect(message).not.toContain("input");
  expect(message).not.toContain("do-not-leak");
  expect(message).not.toContain("[object Object]");
  expect(message.length).toBeLessThanOrEqual("Control API returned 422: ".length + 256);
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
        authority_revision: "a".repeat(64),
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
    authority_revision: "a".repeat(64),
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
  // Break caught: the visual Library falls back to retired evidence routes,
  // action apply bypasses its server preview digest, or one selected owner is
  // replaced by a browser-invented group.
  const requests: Request[] = [];
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    const path = new URL(request.url).pathname;
    if (path === "/api/v1/library") return new Response(JSON.stringify({schema_version: 1, generated_at: "2026-08-15T12:00:00Z", freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20}, models: [], unlinked_recipes: [], next_cursor: null}), {status: 200});
    if (path === "/api/v1/library/recipes/recipe%2Fone") return new Response(JSON.stringify({schema_version: 2, generated_at: "2026-08-15T12:00:00Z", recipe: {recipe_id: "recipe/one", publisher: "vonk-forge", slug: "one", title: "One", description: "", content_sha256: "a".repeat(64)}, definition: {}, model_documents: [], operational_state: {builds: [], mappings: [], installations: [], runs: []}, placement: [], reasons: [], topology: null}), {status: 200});
    if (path.startsWith("/api/v1/jobs/")) return new Response(JSON.stringify({id: "job-1", kind: "recipe.install", state: "running", authority_revision: "a".repeat(64), current_attempt: 1, operations: [], operation_total: 0, targets: [], target_total: 0, progress: {completed: 0, failed: 0, running: 1, total: 1}}), {status: 200});
    return new Response(JSON.stringify({
      id: "operation-1", kind: "recipe.install", owner_id: "owner-1", state: "queued",
      plan_digest: "plan-1", nodes: ["node-a", "node-b"], result: null,
      allowed: true, blockers: [], warnings: [],
    }), {status: request.method === "GET" ? 200 : request.url.includes("preview") ? 200 : 202});
  });
  const api = new ApiClient();
  const controller = new AbortController();

  await api.librarySnapshot("cursor-1");
  await api.libraryRecipe("recipe/one");
  await api.previewLibraryBuild({recipe_revision_id: "revision-1", builder_node_id: "node-a"}, controller.signal);
  await api.applyLibraryBuild({recipe_revision_id: "revision-1", builder_node_id: "node-a", build_input_sha256: "build-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.previewLibraryMapping({recipe_revision_id: "revision-1", node_ids: ["node-a", "node-b"], parameters: {tensor: 2}}, controller.signal);
  await api.applyLibraryMapping({recipe_revision_id: "revision-1", node_ids: ["node-a", "node-b"], parameters: {tensor: 2}, placement_digest: "map-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.previewLibraryImageDistribution({recipe_build_id: "build-1", mapping_id: "mapping-1", mapping_generation: 2});
  await api.applyLibraryImageDistribution({recipe_build_id: "build-1", mapping_id: "mapping-1", mapping_generation: 2, plan_digest: "distribution-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.previewLibraryInstall({recipe_build_id: "build-1", mapping_id: "mapping-1"});
  await api.applyLibraryInstall({recipe_build_id: "build-1", mapping_id: "mapping-1", plan_digest: "install-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.previewLibraryLoad({installation_id: "installation-1", alias: "chat"});
  await api.applyLibraryLoad({installation_id: "installation-1", alias: "chat", plan_digest: "load-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.previewLibraryStop("run-1");
  await api.applyLibraryStop("run-1", {plan_digest: "stop-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.previewLibraryUninstall("installation-1");
  await api.applyLibraryUninstall("installation-1", {plan_digest: "remove-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  await api.libraryOperation("operation-1", controller.signal);
  await api.retryLibraryOperation("operation-1");
  await api.libraryRunStatus("run-1");
  await api.libraryJobProgress("job-1", controller.signal);
  controller.abort();

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/api/v1/library"],
    ["GET", "/api/v1/library/recipes/recipe%2Fone"],
    ["POST", "/api/v1/recipes/build-plans/preview"],
    ["POST", "/api/v1/recipes/builds"],
    ["POST", "/api/v1/recipes/mapping-plans/preview"],
    ["POST", "/api/v1/recipes/mappings"],
    ["POST", "/api/v1/recipes/image-distribution-plans/preview"],
    ["POST", "/api/v1/recipes/image-distributions"],
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
  expect(await requests[3].clone().json()).toEqual({recipe_revision_id: "revision-1", builder_node_id: "node-a", build_input_sha256: "build-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[5].clone().json()).toEqual({recipe_revision_id: "revision-1", node_ids: ["node-a", "node-b"], parameters: {tensor: 2}, placement_digest: "map-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[7].clone().json()).toEqual({recipe_build_id: "build-1", mapping_id: "mapping-1", mapping_generation: 2, plan_digest: "distribution-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[9].clone().json()).toEqual({recipe_build_id: "build-1", mapping_id: "mapping-1", plan_digest: "install-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[10].clone().json()).toEqual({installation_id: "installation-1", alias: "chat"});
  expect(await requests[11].clone().json()).toEqual({installation_id: "installation-1", alias: "chat", plan_digest: "load-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[13].clone().json()).toEqual({plan_digest: "stop-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(await requests[15].clone().json()).toEqual({plan_digest: "remove-plan", request_key: "00000000-0000-4000-8000-000000000001"});
  expect(requests[2].signal.aborted).toBe(true);
  expect(requests[16].signal.aborted).toBe(true);
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
    "raw",
    360,
  );

  const url = new URL(captured!.url);
  expect(url.pathname).toBe("/api/v1/nodes/spk_0123456789abcdef0123456789abcdef/telemetry");
  expect(Object.fromEntries(url.searchParams)).toEqual({
    end: "2026-08-15T12:00:00.000Z",
    maximum_points: "360",
    resolution: "raw",
    start: "2026-08-15T11:00:00.000Z",
  });
});

it("binds the one-click run switch, NAS cache, and rich telemetry routes", async () => {
  // Break caught: a primary Run action falls back to the retired lifecycle
  // routes, cache actions lose their digest/key contract, or telemetry reads
  // silently use the old aggregate endpoint.
  const requests: Request[] = [];
  const modelDigest = "a".repeat(64);
  const artifactDigest = "b".repeat(64);
  const requestKey = "00000000-0000-4000-8000-000000000401";
  const runInput: RunSwitchPreviewRequest = {
    schema_version: 2,
    model_version_sha256: modelDigest,
    recipe_revision_id: "revision-run",
    spark_group: {nodes: [{node_id: "spark-a", rank: 0, role: "leader", endpoint_owner: true}, {node_id: "spark-b", rank: 1, role: "worker", endpoint_owner: false}]},
    alias: "chat",
    action: "run",
    retention: "retain-cached",
    invocation: {origin: "web.library"},
  };
  const response = async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request.clone());
    return new Response(JSON.stringify({}), {headers: {"Content-Type": "application/json"}, status: request.method === "POST" ? 202 : 200});
  };
  vi.stubGlobal("fetch", response);

  const api = new ApiClient();
  await api.previewRecipeRunSwitch(runInput);
  await api.applyRecipeRunSwitch({...runInput, plan_digest: "run-plan", request_key: requestKey});
  await api.getRecipeRunSwitchOperation("run-operation");
  await api.modelCacheInventory("cache-cursor");
  await api.modelCacheEntry(artifactDigest);
  await api.previewModelCacheDownload({schema_version: 2, artifact_set_sha256: artifactDigest, source_policy: "nas-first"});
  await api.downloadModelCache({schema_version: 2, artifact_set_sha256: artifactDigest, source_policy: "nas-first", plan_digest: "cache-download-plan", request_key: requestKey});
  await api.previewModelCacheRepair({schema_version: 2, artifact_set_sha256: artifactDigest});
  await api.repairModelCache({schema_version: 2, artifact_set_sha256: artifactDigest, plan_digest: "cache-repair-plan", request_key: requestKey, source_policy: "nas-first"});
  await api.previewModelCacheEviction({schema_version: 2, target_bytes: 1024});
  await api.evictModelCache({schema_version: 2, target_bytes: 1024, plan_digest: "cache-eviction-plan", request_key: requestKey});
  await api.modelCacheUpdates();
  await api.modelCacheOperations("operations-cursor");
  await api.modelCacheOperation("cache-operation");
  await api.checkModelCacheAccessAndResume("cache-operation", {schema_version: 2, artifact_set_sha256: artifactDigest, plan_digest: "access-plan", request_key: requestKey});
  await api.recipeAvailabilityStart({recipe_revision_id: "recipe-revision-1", request_key: requestKey, force: false});
  await api.recipeAvailabilityList("recipe-revision-1", "running", "recipe-cursor");
  await api.recipeAvailabilityOperation("recipe-operation");
  await api.retryRecipeAvailability("recipe-operation", {request_key: requestKey});
  await api.nodeTelemetryCurrent("spark-a");
  await api.nodeTelemetryCapabilities("spark-a");
  await api.nodeTelemetryWorkloads("spark-a", "run-operation", "running");

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["POST", "/api/v1/recipes/run-switch-plans/preview"],
    ["POST", "/api/v1/recipes/run-switches"],
    ["GET", "/api/v1/recipes/run-switches/run-operation"],
    ["GET", "/api/v1/model-cache"],
    ["GET", `/api/v1/model-cache/entries/${artifactDigest}`],
    ["POST", "/api/v1/model-cache/download-preview"],
    ["POST", "/api/v1/model-cache/download"],
    ["POST", "/api/v1/model-cache/repair-preview"],
    ["POST", "/api/v1/model-cache/repair"],
    ["POST", "/api/v1/model-cache/eviction-preview"],
    ["POST", "/api/v1/model-cache/evict"],
    ["GET", "/api/v1/model-cache/updates"],
    ["GET", "/api/v1/model-cache/operations"],
    ["GET", "/api/v1/model-cache/operations/cache-operation"],
    ["POST", "/api/v1/model-cache/operations/cache-operation/check-access-and-resume"],
    ["POST", "/api/v1/library/recipe-image-availability"],
    ["GET", "/api/v1/library/recipe-image-availability"],
    ["GET", "/api/v1/library/recipe-image-availability/recipe-operation"],
    ["POST", "/api/v1/library/recipe-image-availability/recipe-operation/retry"],
    ["GET", "/api/v1/nodes/spark-a/telemetry/current"],
    ["GET", "/api/v1/nodes/spark-a/telemetry/capabilities"],
    ["GET", "/api/v1/nodes/spark-a/telemetry/workloads"],
  ]);
  expect(Object.fromEntries(new URL(requests[3]!.url).searchParams)).toEqual({cursor: "cache-cursor", limit: "100"});
  expect(Object.fromEntries(new URL(requests[12]!.url).searchParams)).toEqual({cursor: "operations-cursor", limit: "100"});
  expect(Object.fromEntries(new URL(requests[21]!.url).searchParams)).toEqual({run_id: "run-operation", state: "running"});
  expect(await requests[0]!.clone().json()).toEqual(runInput);
  expect(await requests[1]!.clone().json()).toEqual({...runInput, plan_digest: "run-plan", request_key: requestKey});
  expect(await requests[6]!.clone().json()).toEqual({schema_version: 2, artifact_set_sha256: artifactDigest, source_policy: "nas-first", plan_digest: "cache-download-plan", request_key: requestKey});
  expect(await requests[8]!.clone().json()).toEqual({schema_version: 2, artifact_set_sha256: artifactDigest, plan_digest: "cache-repair-plan", request_key: requestKey, source_policy: "nas-first"});
  expect(Object.fromEntries(new URL(requests[16]!.url).searchParams)).toEqual({cursor: "recipe-cursor", limit: "100", recipe_revision_id: "recipe-revision-1", state: "running"});
  expect(requests.slice(0, 2).every(request => request.headers.get("Content-Type"))).toBe(true);
});

it("uses the durable retry endpoints for Run and NAS cache operations", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request.clone());
    return new Response(JSON.stringify({}), {headers: {"Content-Type": "application/json"}, status: 202});
  });
  const api = new ApiClient();
  const runKey = "00000000-0000-4000-8000-000000000402";
  const cacheKey = "00000000-0000-4000-8000-000000000403";

  await api.retryRecipeRunSwitch("run-operation", {schema_version: 2, request_key: runKey});
  await api.retryModelCacheOperation("cache-operation", {schema_version: 2, request_key: cacheKey});
  await api.retryRecipeAvailability("recipe-operation", {request_key: cacheKey});

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["POST", "/api/v1/recipes/run-switches/run-operation/retry"],
    ["POST", "/api/v1/model-cache/operations/cache-operation/retry"],
    ["POST", "/api/v1/library/recipe-image-availability/recipe-operation/retry"],
  ]);
  expect(await requests[0]!.clone().json()).toEqual({schema_version: 2, request_key: runKey});
  expect(await requests[1]!.clone().json()).toEqual({schema_version: 2, request_key: cacheKey});
});

it("uses the durable artifact-job routes and preserves raw upload authority", async () => {
  const requests: Request[] = [];
  let uploadBody: BodyInit | null | undefined;
  document.cookie = "vonk_csrf=artifact-csrf; path=/";
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    return new Response(JSON.stringify({jobs: []}), {headers: {"Content-Type": "application/json"}, status: 200});
  });
  vi.stubGlobal("XMLHttpRequest", class {
    readonly upload: {onprogress: ((event: ProgressEvent) => void) | null} = {onprogress: null};
    onabort: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onload: (() => void) | null = null;
    response: unknown = {id: "job-1", state: "draft"};
    responseType = "";
    status = 200;
    withCredentials = false;
    #headers = new Headers();
    #method = "";
    #url = "";
    open(method: string, url: string) { this.#method = method; this.#url = url; }
    setRequestHeader(name: string, value: string) { this.#headers.set(name, value); }
    send(body: BodyInit | null) {
      uploadBody = body;
      requests.push(new Request(new URL(this.#url, location.origin), {method: this.#method, headers: this.#headers}));
      const size = body instanceof Blob ? body.size : 0;
      this.upload.onprogress?.(new ProgressEvent("progress", {lengthComputable: true, loaded: size, total: size}));
      queueMicrotask(() => this.onload?.());
    }
    abort() { this.onabort?.(); }
  });
  const api = new ApiClient();
  const file = {slot: "prompt", name: "prompt.txt", media_type: "text/plain", size_bytes: 5, sha256: "a".repeat(64)};
  const create = {
    interface: "image-job" as const,
    parameters: {steps: 24},
    inputs: [file],
    output_limits: {max_files: 8, max_file_bytes: 1024, max_total_bytes: 2048, allowed_media_types: ["image/png"]},
    timeout_seconds: 3600,
  };

  await api.artifactJobCapabilities();
  await api.artifactJobsForRun("run-1");
  await api.createArtifactJob("run-1", create);
  const blob = new Blob(["hello"], {type: "text/plain"});
  const progress = vi.fn();
  await api.uploadArtifactJobInput("job-1", file, blob, undefined, progress);
  await api.finalizeArtifactJob("job-1");
  await api.submitArtifactJob("job-1");
  await api.artifactJob("job-1");
  await api.cancelArtifactJob("job-1", "Operator cancelled");
  await api.artifactJobResult("job-1");

  expect(requests.map(request => [request.method, new URL(request.url).pathname])).toEqual([
    ["GET", "/api/v1/artifact-jobs/capabilities"],
    ["GET", "/api/v1/recipes/runs/run-1/artifact-jobs"],
    ["POST", "/api/v1/recipes/runs/run-1/artifact-jobs"],
    ["PUT", "/api/v1/artifact-jobs/job-1/inputs/prompt.txt"],
    ["POST", "/api/v1/artifact-jobs/job-1/finalize"],
    ["POST", "/api/v1/artifact-jobs/job-1/submit"],
    ["GET", "/api/v1/artifact-jobs/job-1"],
    ["POST", "/api/v1/artifact-jobs/job-1/cancel"],
    ["GET", "/api/v1/artifact-jobs/job-1/result"],
  ]);
  expect(requests[3].headers.get("Content-Type")).toBe("text/plain");
  expect(requests[3].headers.get("X-Content-SHA256")).toBe(file.sha256);
  expect(requests[3].headers.get("X-CSRF-Token")).toBe("artifact-csrf");
  expect(uploadBody).toBe(blob);
  expect(progress).toHaveBeenCalledWith({loaded: 5, total: 5});
  expect(api.artifactJobResultUrl("job-1", "b".repeat(64))).toBe(`/api/v1/artifact-jobs/job-1/results/${"b".repeat(64)}`);
  expect(() => api.artifactJobResultUrl("job-1", "../unsafe")).toThrow("Unsafe artifact result digest");
});

it("aborts an in-flight artifact upload without reading the Blob into JavaScript memory", async () => {
  let aborted = false;
  let sentBody: BodyInit | null = null;
  vi.stubGlobal("XMLHttpRequest", class {
    readonly upload = {onprogress: null};
    onabort: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onload: (() => void) | null = null;
    response: unknown = null;
    responseType = "";
    status = 0;
    withCredentials = false;
    open() {}
    setRequestHeader() {}
    send(body: BodyInit | null) { sentBody = body; }
    abort() { aborted = true; this.onabort?.(); }
  });
  const controller = new AbortController();
  const blob = new Blob([new Uint8Array(16 * 1024 * 1024)], {type: "audio/wav"});
  const pending = new ApiClient().uploadArtifactJobInput("job-1", {
    slot: "audio", name: "foley.wav", media_type: "audio/wav", size_bytes: blob.size, sha256: "a".repeat(64),
  }, blob, controller.signal);

  controller.abort();

  await expect(pending).rejects.toMatchObject({name: "AbortError"});
  expect(aborted).toBe(true);
  expect(sentBody).toBe(blob);
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
    return new Response(JSON.stringify({expires_at: "2026-08-05T10:15:00Z", id: "grant-001", purpose: "new-node", token: "g".repeat(48), controller_endpoint: "https://controller.example", enrollment_endpoint: "https://enroll.example", ca_fingerprint: "a".repeat(64), installer_url: "https://install.vonkforge.ai/spark"}), {headers: {"Content-Type": "application/json"}, status: 201});
  });
  await new ApiClient().createEnrollmentGrant(300);
  expect(captured!.method).toBe("POST");
  expect(captured!.headers.get("X-CSRF-Token")).toBe("csrf-value");
  expect(captured!.credentials).toBe("same-origin");
});

it("sends an explicit node-bound re-enrollment grant request", async () => {
  let captured: Request | undefined;
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    captured = input as Request;
    return new Response(JSON.stringify({
      expires_at: "2026-08-05T10:15:00Z", id: "grant-reenroll", purpose: "re-enroll", token: "g".repeat(48),
      controller_endpoint: "https://controller.example", enrollment_endpoint: "https://enroll.example",
      ca_fingerprint: "a".repeat(64), installer_url: "https://install.vonkforge.ai/dev/spark",
    }), {headers: {"Content-Type": "application/json"}, status: 201});
  });

  await new ApiClient().createReenrollmentGrant("spk_0123456789abcdef0123456789abcdef", 300);

  expect(await captured!.json()).toEqual({
    node_id: "spk_0123456789abcdef0123456789abcdef",
    purpose: "re-enroll",
    ttl_seconds: 300,
  });
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
        purpose: "new-node",
        token: "g".repeat(48),
        controller_endpoint: "https://controller.example",
        enrollment_endpoint: "https://enroll.example",
        ca_fingerprint: "a".repeat(64),
        installer_url: "https://install.vonkforge.ai/spark",
      }), {headers: {"Content-Type": "application/json"}, status: 201});
    });

    await new ApiClient().createEnrollmentGrant(300);

    expect(captured!.headers.get("X-CSRF-Token")).toBe(csrfValue);
  },
);

it("does not expose orphaned package and deployment helpers after the Fleet/Library cleanup", () => {
  // Break caught: superseded package/deployment client helpers remain on the
  // live web API surface after their last retained consumers were removed.
  const api: object = new ApiClient();

  for (const name of [
    "packageFamilies",
    "packageCandidates",
    "packageCandidate",
    "previewPackageValidation",
    "validatePackage",
    "packageValidation",
    "previewPackagePromotion",
    "promotePackage",
    "deployments",
    "previewPackage" + "Rollout",
    "startPackage" + "Rollout",
    "packageRollout",
    "previewPackageRollback",
    "rollbackPackage",
    "packageInventory",
    "previewPackageRemoval",
    "removePackageInventory",
    "previewPackageGc",
    "applyPackageGc",
  ]) {
    expect(name in api).toBe(false);
  }
});

it("previews, applies, and reads one durable atomic Library placement", async () => {
  document.cookie = "vonk_csrf=placement-csrf; path=/";
  const recipeId = "00000000-0000-4000-8000-000000000201";
  const revisionId = "00000000-0000-4000-8000-000000000202";
  const placementId = "00000000-0000-4000-8000-000000000203";
  const requestKey = "00000000-0000-4000-8000-000000000204";
  const nodeIds = [`spk_${"a".repeat(32)}`, `spk_${"b".repeat(32)}`];
  const intent = {alias: null, desired_state: "installed" as const, invocation: "drag-drop" as const, node_ids: nodeIds, recipe_id: recipeId};
  const preview = {
    schema_version: 1, generated_at: "2026-09-01T12:00:00Z", recipe_id: recipeId, recipe_revision_id: revisionId,
    recipe_title: "Tiny model", topology_name: "pair", desired_state: "installed", alias: null, invocation: "drag-drop",
    selected_node_ids: nodeIds, selected_nodes: [], allowed: true, steps: [], blockers: [], warnings: [],
    locations: {installation_ids: [], run_ids: [], installed: false, running: false}, plan_digest: "d".repeat(64),
  };
  const application = {
    schema_version: 1, id: placementId, state: "queued", recipe_id: recipeId, recipe_revision_id: revisionId,
    selected_node_ids: nodeIds, desired_state: "installed", alias: null, plan_digest: preview.plan_digest,
    current_step: 0, total_steps: 0, current_operation_id: null, status_reason: null, progress: {}, locations: preview.locations,
    created_at: "2026-09-01T12:00:00Z", updated_at: "2026-09-01T12:00:00Z",
  };
  const captured: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    captured.push(request.clone());
    const body = request.url.endsWith("/preview") ? preview : application;
    return new Response(JSON.stringify(body), {headers: {"Content-Type": "application/json"}, status: request.method === "POST" && !request.url.endsWith("/preview") ? 202 : 200});
  });

  const api = new ApiClient();
  expect(await api.previewLibraryPlacement(intent)).toEqual(preview);
  expect(await api.applyLibraryPlacement({...intent, plan_digest: preview.plan_digest, request_key: requestKey})).toEqual(application);
  expect(await api.libraryPlacement(placementId)).toEqual(application);

  expect(captured.map(request => [new URL(request.url).pathname, request.method])).toEqual([
    ["/api/v1/library/placements/preview", "POST"],
    ["/api/v1/library/placements", "POST"],
    [`/api/v1/library/placements/${placementId}`, "GET"],
  ]);
  expect(await captured[0]!.json()).toEqual(intent);
  expect(await captured[1]!.json()).toEqual({...intent, plan_digest: preview.plan_digest, request_key: requestKey});
  expect(captured[0]!.headers.get("X-CSRF-Token")).toBe("placement-csrf");
  expect(captured[1]!.headers.get("X-CSRF-Token")).toBe("placement-csrf");
});

it("previews and applies one digest-bound fleet-wide model deletion", async () => {
  document.cookie = "vonk_csrf=model-delete-csrf; path=/";
  const modelDigest = "e".repeat(64);
  const requestKey = "00000000-0000-4000-8000-000000000205";
  const plan = {
    active_run_count: 0, active_runs: [], allowed: true, blockers: [], bytes_removed: 120 * 1024 ** 3,
    installations: [{installation_id: "installation-chat", installed_bytes: 120 * 1024 ** 3, node_ids: ["node-alpha", "node-beta"], recipe_content_sha256: "a".repeat(64), recipe_id: "recipe-chat", recipe_revision_id: "revision-chat"}],
    model_title: "Qwen 3 BF16", model_version_sha256: modelDigest,
    nodes: [{installation_ids: ["installation-chat"], installed_bytes: 60 * 1024 ** 3, node_id: "node-alpha", recipe_ids: ["recipe-chat"]}, {installation_ids: ["installation-chat"], installed_bytes: 60 * 1024 ** 3, node_id: "node-beta", recipe_ids: ["recipe-chat"]}],
    plan_digest: "model-delete-plan", shared_cache_policy: "Unrelated immutable caches remain installed.", warnings: [],
  };
  const operation = {id: "operation-model-delete", kind: "model-delete", owner_id: modelDigest, state: "queued", plan_digest: plan.plan_digest, nodes: ["node-alpha", "node-beta"], result: null};
  const captured: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    captured.push(request.clone());
    return new Response(JSON.stringify(request.url.endsWith("/preview") ? plan : operation), {headers: {"Content-Type": "application/json"}, status: request.url.endsWith("/preview") ? 200 : 202});
  });

  const api = new ApiClient();
  expect(await api.previewLibraryModelDeletion(modelDigest)).toEqual(plan);
  expect(await api.deleteLibraryModel(modelDigest, {plan_digest: plan.plan_digest, request_key: requestKey})).toEqual(operation);

  expect(captured.map(request => [new URL(request.url).pathname, request.method])).toEqual([
    ["/api/v1/library/model-deletion-plans/preview", "POST"],
    [`/api/v1/library/models/${modelDigest}/delete`, "POST"],
  ]);
  expect(await captured[0]!.json()).toEqual({model_version_sha256: modelDigest});
  expect(await captured[1]!.json()).toEqual({plan_digest: plan.plan_digest, request_key: requestKey});
  expect(captured[0]!.headers.get("X-CSRF-Token")).toBe("model-delete-csrf");
  expect(captured[1]!.headers.get("X-CSRF-Token")).toBe("model-delete-csrf");
});

it("previews and applies an exact repair manifest through browser CSRF auth", async () => {
  document.cookie = "vonk_csrf=repair-csrf; path=/";
  const captured: Request[] = [];
  const packageDescriptor = REPAIR_MANIFEST.package;
  const plan: AgentUpgradePlan = {
    authority_revision: "c".repeat(64),
    node_ids: [REPAIR_NODE],
    package: packageDescriptor,
    plan_digest: "d".repeat(64),
    repair_manifest: REPAIR_MANIFEST,
    strategy: "one-at-a-time",
  };
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    captured.push(request);
    return new Response(JSON.stringify(captured.length === 1 ? plan : {id: "repair-job", state: "queued"}), {
      headers: {"Content-Type": "application/json"},
      status: captured.length === 1 ? 200 : 202,
    });
  });
  const api = new ApiClient();

  expect(await api.previewAgentUpgrade(REPAIR_NODE ? [REPAIR_NODE] : undefined, "one-at-a-time", REPAIR_MANIFEST)).toEqual(plan);
  await api.applyAgentUpgrade(plan);

  expect(captured).toHaveLength(2);
  expect(captured.every(request => request.headers.get("X-CSRF-Token") === "repair-csrf")).toBe(true);
  expect(captured.every(request => request.credentials === "same-origin")).toBe(true);
  expect(await captured[0].json()).toEqual({
    node_ids: [REPAIR_NODE],
    repair_manifest: REPAIR_MANIFEST,
    strategy: "one-at-a-time",
  });
  expect(await captured[1].json()).toEqual({
    node_ids: [REPAIR_NODE],
    plan_digest: plan.plan_digest,
    repair_manifest: REPAIR_MANIFEST,
    strategy: "one-at-a-time",
  });
});

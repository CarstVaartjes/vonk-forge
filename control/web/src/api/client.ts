import createClient from "openapi-fetch";
import {AuthenticationRequired} from "../auth";
import type {paths} from "./generated";
import type {
  AuthSession,
  AgentsResponse,
  AuditResponse,
  AuditSummary,
  ControlApi,
  EnrollmentDecisionResponse,
  EnrollmentGrantResponse,
  EnrollmentListResponse,
  FleetEvidenceResponse,
  JobDetail,
  JobResumeResponse,
  JobsResponse,
  ProposalInput,
  ProposalPreview,
  CatalogRecipeDocument,
  CatalogRecipeList,
  CatalogRecipeRevision,
  GlobalRecipeRevision,
  PublicRecipeList,
  PublicRecipePreview,
  WorkloadRunApplied,
  WorkloadRunPreview,
  TelemetryHistory,
  TelemetryResolution,
  VisualFleetSnapshot,
  SourceBundleReceipt,
  SourcePolicyReport,
  RecipeBuildPlan,
  RecipeMappingPlan,
  RecipeOperation,
  LibraryInstallApplyInput,
  LibraryInstallPreviewInput,
  LibraryLoadApplyInput,
  LibraryLoadPreviewInput,
  LibraryMappingApplyInput,
  LibraryMappingPreviewInput,
  LibraryStopApplyInput,
  LibraryUninstallApplyInput,
} from "./types";

function csrfToken(): string | undefined {
  const cookie = document.cookie
    .split(";")
    .map(value => value.trim())
    .find(value => value.startsWith("vonk_csrf="));
  return cookie?.slice(cookie.indexOf("=") + 1);
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

const API_DETAIL_LIMIT = 256;

function formatValidationDetail(detail: unknown): string | undefined {
  if (typeof detail !== "object" || detail === null || Array.isArray(detail)) return undefined;
  const record = detail as Record<string, unknown>;
  const location = Array.isArray(record.loc)
    ? record.loc
      .filter((part): part is string | number => typeof part === "string" || typeof part === "number")
      .map(String)
      .join(".")
    : "";
  const message = typeof record.msg === "string" && record.msg.length > 0
    ? record.msg
    : typeof record.type === "string" && record.type.length > 0
      ? record.type
      : "";
  if (!location && !message) return undefined;
  return [location, message].filter(Boolean).join(": ");
}

function formatApiDetail(detail: unknown): string {
  if (typeof detail === "string") return detail.slice(0, API_DETAIL_LIMIT);
  if (Array.isArray(detail)) {
    const validationDetails = detail.map(formatValidationDetail).filter((value): value is string => value !== undefined);
    if (validationDetails.length > 0) return validationDetails.join("\n").slice(0, API_DETAIL_LIMIT);
  }
  try {
    const formatted = JSON.stringify(detail, (key, value) => key === "input" ? undefined : value);
    if (typeof formatted === "string") return formatted.slice(0, API_DETAIL_LIMIT);
  } catch {
    // Fall through to a stable message for values JSON cannot represent.
  }
  return "request failed";
}

function resultData<T>(result: {data?: T; error?: unknown; response: Response}): T {
  if (result.data === undefined) {
    const detail = typeof result.error === "object" && result.error !== null && "detail" in result.error
      ? formatApiDetail(result.error.detail)
      : "request failed";
    throw new Error(`Control API returned ${result.response.status}: ${detail}`);
  }
  return result.data;
}

export class ApiClient implements ControlApi {
  private authenticationRequired?: () => void;
  private readonly generated = createClient<paths>({
    baseUrl: location.origin,
    credentials: "same-origin",
    headers: {Accept: "application/json"},
  });

  constructor() {
    this.generated.use({
      onRequest({request}) {
        if (["GET", "HEAD"].includes(request.method)) return;
        const csrf = csrfToken();
        if (!csrf) return;
        const headers = new Headers(request.headers);
        headers.set("X-CSRF-Token", csrf);
        return new Request(request, {headers});
      },
      onResponse: ({response}) => {
        this.requireAuthentication(response);
      },
    });
  }

  onAuthenticationRequired(listener: () => void): () => void {
    this.authenticationRequired = listener;
    return () => {
      if (this.authenticationRequired === listener) this.authenticationRequired = undefined;
    };
  }

  private requireAuthentication(response: Response): void {
    if (response.status !== 401) return;
    this.authenticationRequired?.();
    throw new AuthenticationRequired();
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!path.startsWith("/api/v1/") || path.includes("..")) throw new Error("Unsafe API path");
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    const csrf = csrfToken();
    if (csrf && init.method && !["GET", "HEAD"].includes(init.method)) headers.set("X-CSRF-Token", csrf);
    const response = await fetch(path, {...init, headers, credentials: "same-origin"});
    this.requireAuthentication(response);
    if (!response.ok) {
      let problem: unknown;
      try { problem = await response.json(); } catch { problem = null; }
      if (typeof problem === "object" && problem !== null) {
        const body = problem as {code?: unknown; detail?: unknown};
        const code = typeof body.code === "string" ? body.code.slice(0, 128) : `HTTP ${response.status}`;
        const detail = typeof body.detail === "string" ? body.detail.slice(0, 256) : "request failed";
        throw new ApiError(response.status, `${code}: ${detail}`);
      }
      throw new ApiError(response.status, `Control API returned ${response.status}`);
    }
    return response.json() as Promise<T>;
  }

  session(): Promise<AuthSession> {
    return this.request("/api/v1/auth/session");
  }

  login(subject: "admin", password: string): Promise<AuthSession> {
    return this.request("/api/v1/auth/login", {method: "POST", body: JSON.stringify({subject, password})});
  }

  async logout(): Promise<void> {
    const headers = new Headers({Accept: "application/json"});
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    const response = await fetch("/api/v1/auth/logout", {method: "POST", headers, credentials: "same-origin"});
    this.requireAuthentication(response);
    if (response.status !== 204) throw new ApiError(response.status, `Control API returned ${response.status}`);
  }

  async catalogRecipes(cursor?: string): Promise<CatalogRecipeList> {
    return resultData(await this.generated.GET("/api/v1/catalog/recipes", {params: {query: {cursor, limit: 20}}}));
  }

  async catalogRecipe(recipeId: string): Promise<CatalogRecipeRevision> {
    return resultData(await this.generated.GET("/api/v1/catalog/recipes/{recipe_id}", {params: {path: {recipe_id: recipeId}}}));
  }

  async createCatalogRecipe(input: {slug: string; document: CatalogRecipeDocument}): Promise<CatalogRecipeRevision> {
    return resultData(await this.generated.POST("/api/v1/catalog/recipes", {body: input}));
  }

  async updateCatalogRecipe(recipeId: string, expectedRevision: number, document: CatalogRecipeDocument): Promise<CatalogRecipeRevision> {
    return resultData(await this.generated.PUT("/api/v1/catalog/recipes/{recipe_id}/draft", {params: {path: {recipe_id: recipeId}}, body: {expected_revision: expectedRevision, document}}));
  }

  async resolveCatalogRecipe(recipeId: string, expectedRevision: number): Promise<CatalogRecipeRevision> {
    return resultData(await this.generated.POST("/api/v1/catalog/recipes/{recipe_id}/resolve", {params: {path: {recipe_id: recipeId}}, body: {expected_revision: expectedRevision}}));
  }

  async forkCatalogRecipe(recipeId: string, revision: number, slug: string): Promise<CatalogRecipeRevision> {
    return resultData(await this.generated.POST("/api/v1/catalog/recipes/{recipe_id}/fork", {params: {path: {recipe_id: recipeId}}, body: {revision, slug}}));
  }

  previewGlobalRecipe(uri: string): Promise<GlobalRecipeRevision> {
    return this.request("/api/v1/catalog/imports/global/preview", {method: "POST", body: JSON.stringify({uri})});
  }

  importGlobalRecipe(uri: string, expectedContentSha256: string): Promise<CatalogRecipeRevision> {
    return this.request("/api/v1/catalog/imports/global", {method: "POST", body: JSON.stringify({uri, expected_content_sha256: expectedContentSha256})});
  }

  listPublicRecipes(): Promise<PublicRecipeList> {
    return this.request("/api/v1/catalog/public-recipes");
  }

  previewPublicRecipe(uri: string): Promise<PublicRecipePreview> {
    return this.request("/api/v1/catalog/imports/public/preview", {method: "POST", body: JSON.stringify({uri})});
  }

  importPublicRecipe(uri: string, expectedContentSha256: string): Promise<CatalogRecipeRevision> {
    return this.request("/api/v1/catalog/imports/public", {method: "POST", body: JSON.stringify({uri, expected_content_sha256: expectedContentSha256})});
  }

  async attachPublicationReport(recipeId: string, report: Record<string, unknown>): Promise<void> {
    await this.request(`/api/v1/catalog/recipes/${encodeURIComponent(recipeId)}/publication-report`, {method: "PUT", body: JSON.stringify({report})});
  }

  publicationExport(recipeId: string, publisher: string): Promise<Record<string, unknown>> {
    return this.request(`/api/v1/catalog/recipes/${encodeURIComponent(recipeId)}/publication-export`, {method: "POST", body: JSON.stringify({publisher})});
  }

  async uploadSourceBundle(sha256: string, archive: Uint8Array): Promise<SourceBundleReceipt> {
    if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error("Invalid source bundle digest");
    const headers = new Headers({Accept: "application/json", "Content-Type": "application/vnd.vonk-forge.source-bundle.v1+tar"});
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    const response = await fetch(`/api/v1/catalog/source-bundles/${sha256}`, {
      method: "PUT", body: archive as BodyInit, headers, credentials: "same-origin",
    });
    this.requireAuthentication(response);
    if (!response.ok) throw new Error(`Source upload returned ${response.status}: ${(await response.text()).slice(0, 256)}`);
    return response.json() as Promise<SourceBundleReceipt>;
  }

  checkRecipeSource(recipeRevisionId: string): Promise<SourcePolicyReport> {
    return this.request("/api/v1/recipes/source-checks", {method: "POST", body: JSON.stringify({recipe_revision_id: recipeRevisionId})});
  }

  previewRecipeBuild(recipeRevisionId: string, builderNodeId: string): Promise<RecipeBuildPlan> {
    return this.request("/api/v1/recipes/build-plans/preview", {method: "POST", body: JSON.stringify({recipe_revision_id: recipeRevisionId, builder_node_id: builderNodeId})});
  }

  buildRecipe(plan: RecipeBuildPlan): Promise<RecipeOperation> {
    return this.request("/api/v1/recipes/builds", {method: "POST", body: JSON.stringify({recipe_revision_id: plan.recipe_revision_id, builder_node_id: plan.builder_node_id, build_input_sha256: plan.build_input_sha256, request_key: crypto.randomUUID()})});
  }

  previewRecipeMapping(recipeRevisionId: string, nodeIds: string[]): Promise<RecipeMappingPlan> {
    return this.request("/api/v1/recipes/mapping-plans/preview", {method: "POST", body: JSON.stringify({recipe_revision_id: recipeRevisionId, node_ids: nodeIds, parameters: {}})});
  }

  createRecipeMapping(plan: RecipeMappingPlan): Promise<{mapping_id: string; generation: number; placement_digest: string}> {
    return this.request("/api/v1/recipes/mappings", {method: "POST", body: JSON.stringify({recipe_revision_id: plan.recipe_revision_id, node_ids: plan.nodes.map(node => node.node_id), parameters: plan.parameters, placement_digest: plan.placement_digest, request_key: crypto.randomUUID()})});
  }

  previewWorkloadRun(sourceYaml: string): Promise<WorkloadRunPreview> {
    return this.request("/api/v1/catalog/imports/workload_run/preview", {method: "POST", body: JSON.stringify({source_yaml: sourceYaml})});
  }

  applyWorkloadRun(sourceYaml: string, sourceSha256: string, reportDigest: string): Promise<WorkloadRunApplied> {
    return this.request("/api/v1/catalog/imports/workload_run", {method: "POST", body: JSON.stringify({source_yaml: sourceYaml, source_sha256: sourceSha256, report_digest: reportDigest})});
  }

  async visualFleet(signal?: AbortSignal): Promise<VisualFleetSnapshot> {
    return resultData(await this.generated.GET("/api/v1/fleet", {signal}));
  }

  async librarySnapshot(cursor?: string, signal?: AbortSignal) {
    return resultData(await this.generated.GET("/api/v1/library", {
      params: {query: {cursor, limit: 100}},
      signal,
    }));
  }

  async libraryRecipe(recipeId: string, signal?: AbortSignal) {
    return resultData(await this.generated.GET("/api/v1/library/recipes/{recipe_id}", {
      params: {path: {recipe_id: recipeId}},
      signal,
    }));
  }

  async previewLibraryMapping(input: LibraryMappingPreviewInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/mapping-plans/preview", {body: input, signal}));
  }

  async applyLibraryMapping(input: LibraryMappingApplyInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/mappings", {
      body: input,
      signal,
    }));
  }

  async previewLibraryInstall(input: LibraryInstallPreviewInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/install-plans/preview", {body: input, signal}));
  }

  async applyLibraryInstall(input: LibraryInstallApplyInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/installations", {
      body: input,
      signal,
    }));
  }

  async previewLibraryLoad(input: LibraryLoadPreviewInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/run-plans/preview", {body: input, signal}));
  }

  async applyLibraryLoad(input: LibraryLoadApplyInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/runs", {
      body: input,
      signal,
    }));
  }

  async previewLibraryStop(runId: string, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/stop-plans/preview", {body: {run_id: runId}, signal}));
  }

  async applyLibraryStop(runId: string, input: LibraryStopApplyInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/runs/{run_id}/stop", {
      params: {path: {run_id: runId}},
      body: input,
      signal,
    }));
  }

  async previewLibraryUninstall(installationId: string, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/uninstall-plans/preview", {
      body: {installation_id: installationId},
      signal,
    }));
  }

  async applyLibraryUninstall(installationId: string, input: LibraryUninstallApplyInput, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/installations/{installation_id}/uninstall", {
      params: {path: {installation_id: installationId}},
      body: input,
      signal,
    }));
  }

  async libraryOperation(operationId: string, signal?: AbortSignal) {
    return resultData(await this.generated.GET("/api/v1/recipes/operations/{operation_id}", {
      params: {path: {operation_id: operationId}},
      signal,
    }));
  }

  async retryLibraryOperation(operationId: string, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/v1/recipes/operations/{operation_id}/retry", {
      params: {path: {operation_id: operationId}},
      body: {request_key: crypto.randomUUID()},
      signal,
    }));
  }

  async libraryRunStatus(runId: string, signal?: AbortSignal) {
    return resultData(await this.generated.GET("/api/v1/recipes/runs/{run_id}", {
      params: {path: {run_id: runId}},
      signal,
    }));
  }

  async libraryJobProgress(jobId: string, signal?: AbortSignal) {
    return resultData(await this.generated.GET("/api/v1/jobs/{job_id}", {
      params: {path: {job_id: jobId}, query: {}},
      signal,
    }));
  }

  async nodeStatuses(signal?: AbortSignal): Promise<FleetEvidenceResponse> {
    return resultData(await this.generated.GET("/api/v1/nodes/status", {signal}));
  }

  fleetEvidence(signal?: AbortSignal): Promise<FleetEvidenceResponse> {
    return this.nodeStatuses(signal);
  }

  async nodeTelemetryHistory(
    nodeId: string,
    start: string,
    end: string,
    resolution: TelemetryResolution,
    maximumPoints: number,
    signal?: AbortSignal,
  ): Promise<TelemetryHistory> {
    return resultData(await this.generated.GET("/api/v1/nodes/{node_id}/telemetry", {
      params: {
        path: {node_id: nodeId},
        query: {start, end, resolution, maximum_points: maximumPoints},
      },
      signal,
    }));
  }

  async agents(): Promise<AgentsResponse> {
    return resultData(await this.generated.GET("/api/v1/agents"));
  }

  async enrollments(): Promise<EnrollmentListResponse> {
    return resultData(await this.generated.GET("/api/v1/agents/enrollments"));
  }

  async createEnrollmentGrant(ttlSeconds: number, signal?: AbortSignal): Promise<EnrollmentGrantResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/grants", {
      body: {ttl_seconds: ttlSeconds},
      signal,
    }));
  }

  async approveEnrollment(enrollmentId: string): Promise<EnrollmentDecisionResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/{enrollment_id}/approve", {
      params: {path: {enrollment_id: enrollmentId}},
    }));
  }

  async rejectEnrollment(enrollmentId: string, reason: string): Promise<EnrollmentDecisionResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/{enrollment_id}/reject", {
      body: {reason},
      params: {path: {enrollment_id: enrollmentId}},
    }));
  }

  async revokeAgentNode(nodeId: string): Promise<void> {
    const {response} = await this.generated.POST("/api/v1/agents/nodes/{node_id}/revoke", {
      params: {path: {node_id: nodeId}},
    });
    if (!response.ok) throw new Error(`Control API returned ${response.status}`);
  }

  async jobs(cursor?: string): Promise<JobsResponse> {
    return resultData(await this.generated.GET("/api/v1/jobs", {
      params: {query: {cursor, limit: 20}},
    }));
  }

  async job(jobId: string, operationCursor?: string, targetCursor?: string): Promise<JobDetail> {
    return resultData(await this.generated.GET("/api/v1/jobs/{job_id}", {
      params: {
        path: {job_id: jobId},
        query: {limit: 20, operation_cursor: operationCursor, target_cursor: targetCursor},
      },
    }));
  }

  async resumeJob(jobId: string): Promise<JobResumeResponse> {
    return resultData(await this.generated.POST("/api/v1/jobs/{job_id}/resume", {
      params: {path: {job_id: jobId}},
    }));
  }

  audit() { return this.request<AuditResponse>("/api/v1/audit"); }
  preview(input: ProposalInput) { return this.request<ProposalPreview>("/api/v1/proposals", {method: "POST", body: JSON.stringify(input)}); }
  submit(digest: string) { return this.request<Record<string, unknown>>("/api/v1/changes", {method: "POST", body: JSON.stringify({proposal_digest: digest})}); }
}

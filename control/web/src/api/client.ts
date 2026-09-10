import createClient from "openapi-fetch";
import {AuthenticationRequired} from "../auth";
import type {paths} from "./generated";
import type {
  AuthSession,
  CliTokenDownload,
  AuditResponse,
  ControlApi,
  FleetProfileInput,
  FleetProfileList,
  FleetProfilePreview,
  FleetProfile,
  FleetProfileApplicationView,
  FleetProfileLoadInput,
  JobDetail,
  JobResumeResponse,
  JobsResponse,
  OperationsResponse,
  OperationDetail,
  VisualFleetSnapshot,
  ArtifactJob,
  ArtifactJobCapabilities,
  ArtifactJobCreateInput,
  ArtifactJobInputFile,
  ArtifactJobList,
  ArtifactTransferProgress,
  ModelDetail,
  ModelLibrary,
  ModelStatus,
  ModelCacheOperatorResponse,
  RecipeDetail,
  RecipeLibrary,
  RecipeStatus,
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
    throw new ApiError(result.response.status, `Control API returned ${result.response.status}: ${detail}`);
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
    if (!path.startsWith("/api/") || path.includes("..")) throw new Error("Unsafe API path");
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
    return this.request("/api/auth/session");
  }

  login(subject: "admin", password: string): Promise<AuthSession> {
    return this.request("/api/auth/login", {method: "POST", body: JSON.stringify({subject, password})});
  }

  async logout(): Promise<void> {
    const headers = new Headers({Accept: "application/json"});
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    const response = await fetch("/api/auth/logout", {method: "POST", headers, credentials: "same-origin"});
    this.requireAuthentication(response);
    if (response.status !== 204) throw new ApiError(response.status, `Control API returned ${response.status}`);
  }

  async downloadCliToken(): Promise<CliTokenDownload> {
    const headers = new Headers({Accept: "text/plain"});
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
    const response = await fetch("/api/auth/cli-token", {method: "POST", headers, credentials: "same-origin"});
    this.requireAuthentication(response);
    if (!response.ok) {
      let problem: unknown;
      try { problem = await response.json(); } catch { problem = null; }
      const detail = typeof problem === "object" && problem !== null && "detail" in problem
        ? formatApiDetail(problem.detail)
        : "request failed";
      throw new ApiError(response.status, `Control API returned ${response.status}: ${detail}`);
    }
    const content = await response.blob();
    if (content.size === 0) throw new ApiError(response.status, "Control API returned an empty CLI token");
    const downloadUrl = URL.createObjectURL(content);
    const anchor = document.createElement("a");
    anchor.href = downloadUrl;
    anchor.download = "vonkctl-token";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    return {expiresAt: response.headers.get("X-Vonk-Token-Expires-At") ?? ""};
  }

  async visualFleet(signal?: AbortSignal): Promise<VisualFleetSnapshot> {
    return resultData(await this.generated.GET("/api/fleet", {signal}));
  }

  async profiles(signal?: AbortSignal): Promise<FleetProfileList> {
    return resultData(await this.generated.GET("/api/profile", {signal}));
  }

  async profile(number: number, signal?: AbortSignal): Promise<FleetProfile> {
    return resultData(await this.generated.GET("/api/profile/{number}", {
      params: {path: {number}},
      signal,
    }));
  }

  async autosaveProfile(number: number, input: FleetProfileInput, signal?: AbortSignal): Promise<FleetProfile> {
    return resultData(await this.generated.PUT("/api/profile/{number}", {
      params: {path: {number}},
      body: input,
      signal,
    }));
  }

  async previewProfile(number: number, signal?: AbortSignal): Promise<FleetProfilePreview> {
    return resultData(await this.generated.POST("/api/profile/{number}/preview", {
      params: {path: {number}},
      signal,
    }));
  }

  async loadProfile(number: number, input: FleetProfileLoadInput = {dry_run: false}, signal?: AbortSignal): Promise<FleetProfileApplicationView> {
    return resultData(await this.generated.POST("/api/profile/{number}/load", {
      params: {path: {number}},
      body: input,
      signal,
    }));
  }

  async profileProgress(number: number, signal?: AbortSignal): Promise<FleetProfileApplicationView> {
    return resultData(await this.generated.GET("/api/profile/{number}/progress", {
      params: {path: {number}},
      signal,
    }));
  }

  async modelStatus(signal?: AbortSignal): Promise<ModelStatus> {
    return resultData(await this.generated.GET("/api/model", {signal}));
  }

  async modelLibrary(cursor?: string, signal?: AbortSignal): Promise<ModelLibrary> {
    return resultData(await this.generated.GET("/api/model/library", {
      params: {query: {cursor, limit: 100}},
      signal,
    }));
  }

  async modelDetail(selector: string, signal?: AbortSignal): Promise<ModelDetail> {
    return resultData(await this.generated.GET("/api/model/{selector}", {
      params: {path: {selector}},
      signal,
    }));
  }

  async recipeStatus(signal?: AbortSignal): Promise<RecipeStatus> {
    return resultData(await this.generated.GET("/api/recipe", {signal}));
  }

  async recipeLibrary(cursor?: string, signal?: AbortSignal): Promise<RecipeLibrary> {
    return resultData(await this.generated.GET("/api/recipe/library", {
      params: {query: {cursor, limit: 100}},
      signal,
    }));
  }

  async recipeDetail(selector: string, signal?: AbortSignal): Promise<RecipeDetail> {
    return resultData(await this.generated.GET("/api/recipe/{selector}", {
      params: {path: {selector}},
      signal,
    }));
  }

  async libraryJobProgress(jobId: string, signal?: AbortSignal) {
    return resultData(await this.generated.GET("/api/jobs/{job_id}", {
      params: {path: {job_id: jobId}, query: {}},
      signal,
    }));
  }

  async prepareModelCache(selector: string, requestKey: string, signal?: AbortSignal): Promise<ModelCacheOperatorResponse> {
    return resultData(await this.generated.POST("/api/model/{selector}/download", {
      params: {path: {selector}},
      body: {request_key: requestKey, schema_version: 2, with_model: false},
      signal,
    }));
  }

  async modelCacheOperation(operationId: string, signal?: AbortSignal): Promise<ModelCacheOperatorResponse> {
    return resultData(await this.generated.GET("/api/model/operations/{operation_id}", {
      params: {path: {operation_id: operationId}},
      signal,
    }));
  }

  artifactJobsForRun(runId: string, signal?: AbortSignal): Promise<ArtifactJobList> {
    return this.request(`/api/recipe/runs/${encodeURIComponent(runId)}/artifact-jobs`, {signal});
  }

  artifactJobCapabilities(signal?: AbortSignal): Promise<ArtifactJobCapabilities> {
    return this.request("/api/artifact-jobs/capabilities", {signal});
  }

  createArtifactJob(runId: string, input: ArtifactJobCreateInput, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/recipe/runs/${encodeURIComponent(runId)}/artifact-jobs`, {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    });
  }

  async uploadArtifactJobInput(jobId: string, file: ArtifactJobInputFile, content: Blob, signal?: AbortSignal, onProgress?: (progress: ArtifactTransferProgress) => void): Promise<ArtifactJob> {
    const path = `/api/artifact-jobs/${encodeURIComponent(jobId)}/inputs/${encodeURIComponent(file.name)}`;
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      const abort = () => request.abort();
      const finish = () => signal?.removeEventListener("abort", abort);
      request.open("PUT", path);
      request.responseType = "json";
      request.withCredentials = true;
      request.setRequestHeader("Accept", "application/json");
      request.setRequestHeader("Content-Type", file.media_type);
      request.setRequestHeader("X-Content-SHA256", file.sha256);
      const csrf = csrfToken();
      if (csrf) request.setRequestHeader("X-CSRF-Token", csrf);
      request.upload.onprogress = event => onProgress?.({loaded: event.loaded, total: event.lengthComputable ? event.total : content.size});
      request.onabort = () => { finish(); reject(new DOMException("Artifact upload cancelled", "AbortError")); };
      request.onerror = () => { finish(); reject(new ApiError(0, "Artifact upload failed before the controller responded")); };
      request.onload = () => {
        finish();
        try { this.requireAuthentication(new Response(null, {status: request.status})); }
        catch (error) { reject(error); return; }
        if (request.status < 200 || request.status >= 300) {
          const response = request.response as {detail?: unknown} | null;
          reject(new ApiError(request.status, response?.detail === undefined ? `Control API returned ${request.status}` : formatApiDetail(response.detail)));
          return;
        }
        resolve(request.response as ArtifactJob);
      };
      if (signal?.aborted) { request.abort(); return; }
      signal?.addEventListener("abort", abort, {once: true});
      request.send(content);
    });
  }

  finalizeArtifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/finalize`, {method: "POST", signal});
  }

  submitArtifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/submit`, {method: "POST", signal});
  }

  artifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}`, {signal});
  }

  cancelArtifactJob(jobId: string, reason: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({reason}),
      signal,
    });
  }

  artifactJobResult(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/result`, {signal});
  }

  artifactJobResultUrl(jobId: string, sha256: string): string {
    if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error("Unsafe artifact result digest");
    return `/api/artifact-jobs/${encodeURIComponent(jobId)}/results/${sha256}`;
  }

  async jobs(cursor?: string): Promise<JobsResponse> {
    return resultData(await this.generated.GET("/api/jobs", {
      params: {query: {cursor, limit: 20}},
    }));
  }

  async operations(cursor?: string, signal?: AbortSignal): Promise<OperationsResponse> {
    return resultData(await this.generated.GET("/api/operations", {
      params: {query: {cursor, limit: 20}}, signal,
    }));
  }

  async operation(operationId: string, signal?: AbortSignal): Promise<OperationDetail> {
    return resultData(await this.generated.GET("/api/operations/{operation_id}", {
      params: {path: {operation_id: operationId}}, signal,
    }));
  }

  async job(jobId: string, operationCursor?: string, targetCursor?: string): Promise<JobDetail> {
    return resultData(await this.generated.GET("/api/jobs/{job_id}", {
      params: {
        path: {job_id: jobId},
        query: {limit: 20, operation_cursor: operationCursor, target_cursor: targetCursor},
      },
    }));
  }

  async resumeJob(jobId: string): Promise<JobResumeResponse> {
    return resultData(await this.generated.POST("/api/jobs/{job_id}/resume", {
      params: {path: {job_id: jobId}},
    }));
  }

  async audit(signal?: AbortSignal): Promise<AuditResponse> {
    return resultData(await this.generated.GET("/api/audit", {signal}));
  }
}

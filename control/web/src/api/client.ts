import { FleetEventConnection } from "./fleet-event-connection";
import { readObservationTransfer } from "./observation-transfer";
import createClient, { createQuerySerializer } from "openapi-fetch";
import {
  ContractResponse,
  readControlResponse,
  readControlResponseText,
  serializeControlBody,
  validateControlBody,
  validateControlParameters,
} from "./contract-json";
import {
  stringifyContractJson,
  parseContractJson,
  isWireNumber,
  formatWire,
} from "./contract-numeric";
import { AuthenticationRequired } from "../auth";
import type { components, paths } from "./generated";
import type {
  AuthSession,
  CliTokenDownload,
  ControlApi,
  FleetProfileInput,
  FleetProfileList,
  FleetProfileEndpoints,
  FleetProfilePreview,
  FleetProfile,
  FleetProfileNumber,
  FleetProfileRead,
  FleetProfileApplicationView,
  FleetProfileLoadInput,
  CatalogSyncStatus,
  ProfileDefinition,
  ReconcilePlan,
  ActivityFilters,
  EnrollmentGrantStatus,
  FleetActionResponse,
  FleetEnrollRequest,
  FleetLogResponse,
  FleetNodeIdentity,
  GatewayKeyCreated,
  GatewayKeyList,
  GatewayKeyRevoked,
  JobDetail,
  JobResumeResponse,
  OperationsResponse,
  OperationDetail,
  VisualFleetNode,
  VisualFleetSnapshot,
  ArtifactJob,
  ArtifactJobCapabilities,
  ArtifactJobCreateInput,
  ArtifactJobInputFile,
  ArtifactJobList,
  ArtifactTransferProgress,
  LibrarySort,
  ModelLibrary,
  ModelCacheOperatorResponse,
  CacheRemovalReview,
  RecipeImageAvailabilityResponse,
  RecipeCacheOperation,
  RecipeOperatorResponse,
  RecipeUpdateResponse,
  RecipeDetail,
  RecipeLibrary,
} from "./types";

function csrfToken(): string | undefined {
  const cookie = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith("vonk_csrf="));
  return cookie?.slice(cookie.indexOf("=") + 1);
}

const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

/** The Controller's correlation ID for a response, when it sent a well-formed one. */
function requestIdOf(source: Response | XMLHttpRequest): string | undefined {
  const value =
    source instanceof Response
      ? source.headers.get("x-request-id")
      : source.getResponseHeader("x-request-id");
  return value !== null && REQUEST_ID.test(value) ? value : undefined;
}

/** A failed Control API call. Its message ends with the request ID so support can find the call. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly requestId?: string,
  ) {
    super(requestId ? `${message} (request ID ${requestId})` : message);
    this.name = "ApiError";
  }
}

const API_DETAIL_LIMIT = 256;

function formatValidationDetail(detail: unknown): string | undefined {
  if (typeof detail !== "object" || detail === null || Array.isArray(detail)) return undefined;
  const record = detail as Record<string, unknown>;
  const location = Array.isArray(record.loc)
    ? record.loc
        .filter(
          (part): part is string | number => typeof part === "string" || typeof part === "number",
        )
        .map(String)
        .join(".")
    : "";
  const message =
    typeof record.msg === "string" && record.msg.length > 0
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
    const validationDetails = detail
      .map(formatValidationDetail)
      .filter((value): value is string => value !== undefined);
    if (validationDetails.length > 0)
      return validationDetails.join("\n").slice(0, API_DETAIL_LIMIT);
  }
  try {
    const formatted = JSON.stringify(detail, (key, value) => (key === "input" ? undefined : value));
    if (typeof formatted === "string") return formatted.slice(0, API_DETAIL_LIMIT);
  } catch {
    // Fall through to a stable message for values JSON cannot represent.
  }
  return "request failed";
}

function gatewayData<T extends GatewayKeyList | GatewayKeyCreated | GatewayKeyRevoked>(result: {
  data?: T | components["schemas"]["UnknownError"];
  error?: unknown;
  response: Response;
}): T {
  const data = resultData(result);
  if ("category" in data)
    throw new ApiError(result.response.status, data.reason, requestIdOf(result.response));
  return data;
}

function resultData<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data === undefined) {
    const detail =
      typeof result.error === "object" && result.error !== null && "detail" in result.error
        ? formatApiDetail(result.error.detail)
        : "request failed";
    throw new ApiError(
      result.response.status,
      `Control API returned ${result.response.status}: ${detail}`,
      requestIdOf(result.response),
    );
  }
  return result.data;
}

export class ApiClient implements ControlApi {
  private authenticationRequired?: () => void;
  private readonly generated = createClient<paths>({
    baseUrl: location.origin,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    bodySerializer: stringifyContractJson,
    querySerializer: (query) =>
      createQuerySerializer()(
        Object.fromEntries(
          Object.entries(query).map(([key, value]) => [
            key,
            Array.isArray(value)
              ? value.map((item) => (isWireNumber(item) ? formatWire(item) : item))
              : isWireNumber(value)
                ? formatWire(value)
                : value,
          ]),
        ),
      ),
  });

  constructor() {
    this.generated.use({
      onRequest: async ({ request, params, schemaPath }) => {
        validateControlParameters(request.method, schemaPath, params);
        const url = new URL(request.url);
        url.pathname = schemaPath.replace(/\{([^}]+)\}/g, (_, key: string) => {
          const value = params.path?.[key];
          if (isWireNumber(value)) return encodeURIComponent(formatWire(value));
          if (typeof value !== "string" && typeof value !== "boolean")
            throw new Error("Unsupported API path parameter");
          return encodeURIComponent(String(value));
        });
        // Request is not a portable WebIDL dictionary: some browser/test
        // runtimes discard its accessor-backed method when used as init.
        // This owner constructs a new URL while preserving every request option.
        const init = {
          method: request.method,
          headers: request.headers,
          body: request.body,
          credentials: request.credentials,
          signal: request.signal,
          redirect: request.redirect,
          mode: request.mode,
          cache: request.cache,
          referrer: request.referrer,
          referrerPolicy: request.referrerPolicy,
          integrity: request.integrity,
          keepalive: request.keepalive,
          // Node's native fetch requires this for a ReadableStream body;
          // browsers ignore unknown dictionary members.
          duplex: "half",
        };
        request = new Request(url, init);
        if (
          request.headers.get("content-type")?.startsWith("application/json") &&
          request.body !== null
        ) {
          const text = await request.clone().text();
          serializeControlBody(request.method, request.url, parseContractJson(text));
        }
        if (!["GET", "HEAD"].includes(request.method)) {
          request.headers.set("X-CSRF-Token", await this.requiredCsrfToken());
        }
        return request;
      },
      onResponse: async ({ request, response }) => {
        let text: string;
        let value: unknown;
        try {
          text = await readControlResponseText(response, request.method, request.url);
          value = validateControlBody(
            request.method,
            request.url,
            response.status,
            response.headers.get("content-type") ?? "",
            text,
          );
        } catch (cause) {
          this.requireAuthentication(response, cause);
          throw cause;
        }
        this.requireAuthentication(response);
        if (!response.ok) {
          const detail =
            typeof value === "object" && value !== null && "detail" in value
              ? formatApiDetail(value.detail)
              : "request failed";
          throw new ApiError(
            response.status,
            `Control API returned ${response.status}: ${detail}`,
            requestIdOf(response),
          );
        }
        if (response.status === 204 || request.method === "HEAD") return response;
        return new ContractResponse(response, value, text);
      },
    });
  }

  onAuthenticationRequired(listener: () => void): () => void {
    this.authenticationRequired = listener;
    return () => {
      if (this.authenticationRequired === listener) this.authenticationRequired = undefined;
    };
  }

  private requireAuthentication(response: Response, cause?: unknown): void {
    if (response.status !== 401) return;
    this.authenticationRequired?.();
    const error = new AuthenticationRequired();
    if (cause !== undefined) error.cause = cause;
    throw error;
  }

  private async requiredCsrfToken(): Promise<string> {
    const token = csrfToken();
    if (token) return token;
    // Expiry removes both cookies. Let the existing session owner distinguish
    // expiry from an authenticated session with a missing CSRF cookie.
    await this.session();
    const refreshed = csrfToken();
    if (!refreshed) throw new Error("CSRF token missing; request not sent");
    return refreshed;
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!path.startsWith("/api/") || path.includes("..")) throw new Error("Unsafe API path");
    // fetch would only reject a body on the implicit GET at send time, with a
    // generic TypeError; refuse the ambiguity up front like other unsafe input.
    if (init.body && init.method === undefined)
      throw new Error("API request with a body requires an explicit method");
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    // Login is the one mutating call the Controller accepts without CSRF (it
    // validates origin instead) and the call that issues the first token.
    const method = (init.method ?? "GET").toUpperCase();
    if (!["GET", "HEAD"].includes(method) && path !== "/api/auth/login")
      headers.set("X-CSRF-Token", await this.requiredCsrfToken());
    if (init.body !== undefined && init.body !== null) {
      if (typeof init.body !== "string")
        throw new Error("JSON API request requires a serialized document");
      serializeControlBody(method, path, parseContractJson(init.body));
    }
    const response = await fetch(path, { ...init, method, headers, credentials: "same-origin" });
    let decoded: unknown;
    try {
      decoded = await readControlResponse(response, method, path);
    } catch (cause) {
      this.requireAuthentication(response, cause);
      throw cause;
    }
    this.requireAuthentication(response);
    if (!response.ok) {
      const problem = decoded;
      if (typeof problem === "object" && problem !== null) {
        const body = problem as { code?: unknown; detail?: unknown };
        const code =
          typeof body.code === "string" ? body.code.slice(0, 128) : `HTTP ${response.status}`;
        const detail =
          typeof body.detail === "string" ? body.detail.slice(0, 256) : "request failed";
        throw new ApiError(response.status, `${code}: ${detail}`, requestIdOf(response));
      }
      throw new ApiError(
        response.status,
        `Control API returned ${response.status}`,
        requestIdOf(response),
      );
    }
    return decoded as T;
  }

  session(): Promise<AuthSession> {
    return this.request("/api/auth/session");
  }

  login(subject: "admin", password: string): Promise<AuthSession> {
    return this.request("/api/auth/login", {
      method: "POST",
      body: stringifyContractJson({ subject, password }),
    });
  }

  async logout(): Promise<void> {
    const headers = new Headers({ Accept: "application/json" });
    headers.set("X-CSRF-Token", await this.requiredCsrfToken());
    const response = await fetch("/api/auth/logout", {
      method: "POST",
      headers,
      credentials: "same-origin",
    });
    try {
      await readControlResponse(response, "POST", "/api/auth/logout");
    } catch (cause) {
      this.requireAuthentication(response, cause);
      throw cause;
    }
    this.requireAuthentication(response);
    if (response.status !== 204)
      throw new ApiError(
        response.status,
        `Control API returned ${response.status}`,
        requestIdOf(response),
      );
  }

  async downloadCliToken(): Promise<CliTokenDownload> {
    const headers = new Headers({ Accept: "text/plain" });
    headers.set("X-CSRF-Token", await this.requiredCsrfToken());
    const response = await fetch("/api/auth/cli-token", {
      method: "POST",
      headers,
      credentials: "same-origin",
    });
    if (!response.ok) {
      let problem: unknown;
      try {
        problem = await readControlResponse(response, "POST", "/api/auth/cli-token");
      } catch (cause) {
        this.requireAuthentication(response, cause);
        throw cause;
      }
      this.requireAuthentication(response);
      const detail =
        typeof problem === "object" && problem !== null && "detail" in problem
          ? formatApiDetail(problem.detail)
          : "request failed";
      throw new ApiError(
        response.status,
        `Control API returned ${response.status}: ${detail}`,
        requestIdOf(response),
      );
    }
    const content = await response.blob();
    if (content.size === 0)
      throw new ApiError(response.status, "Control API returned an empty CLI token");
    const downloadUrl = URL.createObjectURL(content);
    const anchor = document.createElement("a");
    anchor.href = downloadUrl;
    anchor.download = "vonkctl-token";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    return { expires_at: response.headers.get("X-Vonk-Token-Expires-At") ?? "" };
  }

  private async observation(
    path: "/api/fleet" | "/api/platform",
    signal?: AbortSignal,
  ): Promise<unknown> {
    const response = await fetch(path, {
      signal,
      credentials: "same-origin",
      headers: { Accept: "application/x-vonk-observation+ndjson" },
    });
    if (!response.ok) {
      let value: unknown;
      try {
        value = await readControlResponse(response, "GET", path);
      } catch (cause) {
        this.requireAuthentication(response, cause);
        throw cause;
      }
      this.requireAuthentication(response);
      return resultData<unknown>({ response, error: value });
    }
    return readObservationTransfer(response, path);
  }

  fleetEvents(appliedCursor: () => string): FleetEventConnection {
    return new FleetEventConnection(appliedCursor, (response) =>
      this.requireAuthentication(response),
    );
  }

  async visualFleet(signal?: AbortSignal): Promise<VisualFleetSnapshot> {
    return (await this.observation("/api/fleet", signal)) as VisualFleetSnapshot;
  }

  async platformObservation(
    signal?: AbortSignal,
  ): Promise<components["schemas"]["PlatformObservation"]> {
    return (await this.observation(
      "/api/platform",
      signal,
    )) as components["schemas"]["PlatformObservation"];
  }

  async enrollFleetNode(input: FleetEnrollRequest, signal?: AbortSignal) {
    return resultData(await this.generated.POST("/api/fleet/enroll", { body: input, signal }));
  }

  async fleetNode(selector: string, signal?: AbortSignal): Promise<VisualFleetNode> {
    return resultData(
      await this.generated.GET("/api/fleet/{selector}", { params: { path: { selector } }, signal }),
    );
  }

  async renameFleetNode(selector: string, displayName: string): Promise<FleetNodeIdentity> {
    return resultData(
      await this.generated.POST("/api/fleet/{selector}/rename", {
        params: { path: { selector } },
        body: { display_name: displayName },
      }),
    );
  }

  async removeFleetNode(selector: string): Promise<FleetActionResponse> {
    return resultData(
      await this.generated.POST("/api/fleet/{selector}/remove", { params: { path: { selector } } }),
    );
  }

  async reenrollFleetNode(selector: string, requestKey: string): Promise<FleetActionResponse> {
    return resultData(
      await this.generated.POST("/api/fleet/{selector}/re-enroll", {
        params: { path: { selector } },
        body: { request_key: requestKey },
      }),
    );
  }

  async fleetLogs(selector: string, signal?: AbortSignal): Promise<FleetLogResponse> {
    return resultData(
      await this.generated.GET("/api/fleet/{selector}/loginfo", {
        params: { path: { selector }, query: { lines: 100 } },
        signal,
      }),
    );
  }

  async upgradeFleet(
    all: boolean,
    selectors: string[],
    requestKey: string,
  ): Promise<FleetActionResponse> {
    return resultData(
      await this.generated.POST("/api/fleet/upgrade", {
        body: { all, selectors: selectors.length ? selectors : null, request_key: requestKey },
      }),
    );
  }

  async enrollmentStatus(grantId: string, signal?: AbortSignal): Promise<EnrollmentGrantStatus> {
    return resultData(
      await this.generated.GET("/api/fleet/enrollments/{grant_id}", {
        params: { path: { grant_id: grantId } },
        signal,
      }),
    );
  }

  async revokeEnrollment(grantId: string): Promise<EnrollmentGrantStatus> {
    return resultData(
      await this.generated.POST("/api/fleet/enrollments/{grant_id}/revoke", {
        params: { path: { grant_id: grantId } },
      }),
    );
  }

  async gatewayKeys(signal?: AbortSignal): Promise<GatewayKeyList> {
    return gatewayData(await this.generated.GET("/api/key", { signal }));
  }

  async createGatewayKey(
    name: string,
    models: string[],
    expires?: string,
  ): Promise<GatewayKeyCreated> {
    return gatewayData(
      await this.generated.POST("/api/key", { body: { name, models, expires: expires || null } }),
    );
  }

  async rollGatewayKey(name: string): Promise<GatewayKeyCreated> {
    return gatewayData(
      await this.generated.POST("/api/key/{name}/roll", { params: { path: { name } } }),
    );
  }

  async revokeGatewayKey(name: string): Promise<GatewayKeyRevoked> {
    return gatewayData(
      await this.generated.POST("/api/key/{name}/revoke", { params: { path: { name } } }),
    );
  }

  async profiles(signal?: AbortSignal): Promise<FleetProfileList> {
    return resultData(await this.generated.GET("/api/profile", { signal }));
  }

  async profile(number: FleetProfileNumber, signal?: AbortSignal): Promise<FleetProfileRead> {
    return resultData(
      await this.generated.GET("/api/profile/{number}", {
        params: { path: { number } },
        signal,
      }),
    );
  }

  async autosaveProfile(
    number: FleetProfileNumber,
    input: FleetProfileInput,
    signal?: AbortSignal,
  ): Promise<FleetProfile> {
    return resultData(
      await this.generated.PUT("/api/profile/{number}", {
        params: { path: { number } },
        body: input,
        signal,
      }),
    );
  }

  async previewProfile(
    number: FleetProfileNumber,
    signal?: AbortSignal,
  ): Promise<FleetProfilePreview> {
    return resultData(
      await this.generated.POST("/api/profile/{number}/preview", {
        params: { path: { number } },
        signal,
      }),
    );
  }

  async loadProfile(
    number: FleetProfileNumber,
    input: FleetProfileLoadInput,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView> {
    return resultData(
      await this.generated.POST("/api/profile/{number}/load", {
        params: { path: { number } },
        body: input,
        signal,
      }),
    );
  }

  async profileApplicationByRequest(
    number: FleetProfileNumber,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView> {
    return resultData(
      await this.generated.GET("/api/profile/{number}/requests/{request_key}", {
        params: { path: { number, request_key: requestKey } },
        signal,
      }),
    );
  }

  async profileProgress(
    number: FleetProfileNumber,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView> {
    return resultData(
      await this.generated.GET("/api/profile/{number}/progress", {
        params: { path: { number } },
        signal,
      }),
    );
  }

  async profileEndpoints(
    number: FleetProfileNumber,
    signal?: AbortSignal,
  ): Promise<FleetProfileEndpoints> {
    return resultData(
      await this.generated.GET("/api/profile/{number}/endpoints", {
        params: { path: { number } },
        signal,
      }),
    );
  }

  async catalogSyncStatus(signal?: AbortSignal): Promise<CatalogSyncStatus | null> {
    // 404 means the sync has never run, which is a state, not a failure.
    const result = await this.generated.GET("/api/catalog/managed-recipes/sync-status", { signal });
    if (result.response.status === 404) return null;
    return resultData(result);
  }

  async cancelModelOperation(
    operationId: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse> {
    return resultData(
      await this.generated.POST("/api/model/operations/{operation_id}/cancel", {
        params: { path: { operation_id: operationId } },
        body: { request_key: requestKey, reason: "operator requested cancellation" },
        signal,
      }),
    );
  }

  async cancelRecipeOperation(
    operationId: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeCacheOperation> {
    return resultData(
      await this.generated.POST("/api/recipe/operations/{operation_id}/cancel", {
        params: { path: { operation_id: operationId } },
        body: { request_key: requestKey, reason: "operator requested cancellation" },
        signal,
      }),
    );
  }

  async previewInstallationReconcile(
    installationId: string,
    signal?: AbortSignal,
  ): Promise<ReconcilePlan> {
    return resultData(
      await this.generated.POST("/api/recipe/installations/{installation_id}/reconcile/preview", {
        params: { path: { installation_id: installationId } },
        signal,
      }),
    );
  }

  async reconcileInstallation(
    installationId: string,
    requestKey: string,
    planDigest: string,
    signal?: AbortSignal,
  ): Promise<unknown> {
    return resultData(
      await this.generated.POST("/api/recipe/installations/{installation_id}/reconcile", {
        params: { path: { installation_id: installationId } },
        body: { request_key: requestKey, plan_digest: planDigest },
        signal,
      }),
    );
  }

  async profileDefinition(
    number: FleetProfileNumber,
    signal?: AbortSignal,
  ): Promise<ProfileDefinition> {
    return resultData(
      await this.generated.GET("/api/profile/{number}/definition", {
        params: { path: { number } },
        signal,
      }),
    );
  }

  async cancelProfileApplication(
    applicationId: string,
    profileNumber: FleetProfileNumber,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView> {
    return resultData(
      await this.generated.POST("/api/profile/applications/{application_id}/cancel", {
        params: { path: { application_id: applicationId } },
        body: { profile_number: profileNumber, request_key: requestKey },
        signal,
      }),
    );
  }

  async modelLibrary(
    cursor?: string,
    sort?: LibrarySort,
    updatedSince?: string,
    signal?: AbortSignal,
  ): Promise<ModelLibrary> {
    return resultData(
      await this.generated.GET("/api/model/library", {
        params: { query: { cursor, limit: 100, sort, updated_since: updatedSince } },
        signal,
      }),
    );
  }

  async recipeLibrary(
    cursor?: string,
    sort?: LibrarySort,
    updatedSince?: string,
    signal?: AbortSignal,
  ): Promise<RecipeLibrary> {
    return resultData(
      await this.generated.GET("/api/recipe/library", {
        params: { query: { cursor, limit: 100, sort, updated_since: updatedSince } },
        signal,
      }),
    );
  }

  async recipeDetail(selector: string, signal?: AbortSignal): Promise<RecipeDetail> {
    return resultData(
      await this.generated.GET("/api/recipe/{selector}", {
        params: { path: { selector } },
        signal,
      }),
    );
  }

  async prepareModelCache(
    selector: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse> {
    return resultData(
      await this.generated.POST("/api/model/{selector}/download", {
        params: { path: { selector } },
        body: { request_key: requestKey },
        signal,
      }),
    );
  }

  async modelRemovalReview(selector: string, signal?: AbortSignal): Promise<CacheRemovalReview> {
    return resultData(
      await this.generated.GET("/api/model/{selector}/remove-review", {
        params: { path: { selector } },
        signal,
      }),
    );
  }

  async removeModelCache(
    selector: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse> {
    // Removal applies to what the selector resolves to when the Controller accepts it.
    return resultData(
      await this.generated.POST("/api/model/{selector}/remove", {
        params: { path: { selector } },
        body: { request_key: requestKey },
        signal,
      }),
    );
  }

  async modelCacheRequest(
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse> {
    return resultData(
      await this.generated.GET("/api/model/requests/{request_key}", {
        params: { path: { request_key: requestKey } },
        signal,
      }),
    );
  }

  async modelCacheOperation(
    operationId: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse> {
    return resultData(
      await this.generated.GET("/api/model/operations/{operation_id}", {
        params: { path: { operation_id: operationId } },
        signal,
      }),
    );
  }

  async downloadRecipe(
    selector: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeImageAvailabilityResponse> {
    // The recipe download caches the recipe image and its missing model
    // artifacts together, which is what `vonkctl recipe download` documents.
    return resultData(
      await this.generated.POST("/api/recipe/{selector}/download", {
        params: { path: { selector } },
        body: { request_key: requestKey },
        signal,
      }),
    );
  }

  async recipeRemovalReview(
    selector: string,
    withModel: boolean,
    signal?: AbortSignal,
  ): Promise<CacheRemovalReview> {
    return resultData(
      await this.generated.GET("/api/recipe/{selector}/remove-review", {
        params: { path: { selector }, query: { with_model: withModel } },
        signal,
      }),
    );
  }

  async removeRecipe(
    selector: string,
    requestKey: string,
    withModel: boolean,
    signal?: AbortSignal,
  ): Promise<RecipeOperatorResponse> {
    // The model choice is explicit, like the CLI's mandatory --keep-model or
    // --with-model: the Controller fails closed rather than guessing whether a
    // shared model entry should go too.
    return resultData(
      await this.generated.POST("/api/recipe/{selector}/remove", {
        params: { path: { selector } },
        body: { request_key: requestKey, with_model: withModel },
        signal,
      }),
    );
  }

  async updateRecipes(
    all: boolean,
    selectors: string[],
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeUpdateResponse> {
    // Refresh cached recipe images. The Controller requires either a selector
    // or all, and refuses the combination, so the caller states the scope.
    return resultData(
      await this.generated.POST("/api/recipe/update", {
        params: {},
        body: { all, selectors, request_key: requestKey },
        signal,
      }),
    );
  }

  async recipeCacheOperation(
    operationId: string,
    signal?: AbortSignal,
  ): Promise<RecipeCacheOperation> {
    return resultData(
      await this.generated.GET("/api/recipe/operations/{operation_id}", {
        params: { path: { operation_id: operationId } },
        signal,
      }),
    );
  }

  async recipeCacheRequest(
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeCacheOperation> {
    return resultData(
      await this.generated.GET("/api/recipe/requests/{request_key}", {
        params: { path: { request_key: requestKey } },
        signal,
      }),
    );
  }

  artifactJobsForRun(runId: string, signal?: AbortSignal): Promise<ArtifactJobList> {
    return this.request(`/api/recipe/runs/${encodeURIComponent(runId)}/artifact-jobs`, { signal });
  }

  artifactJobByRequestId(requestId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/requests/${encodeURIComponent(requestId)}`, { signal });
  }

  artifactJobCapabilities(signal?: AbortSignal): Promise<ArtifactJobCapabilities> {
    return this.request("/api/artifact-jobs/capabilities", { signal });
  }

  createArtifactJob(
    runId: string,
    input: ArtifactJobCreateInput,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ArtifactJob> {
    return this.request(`/api/recipe/runs/${encodeURIComponent(runId)}/artifact-jobs`, {
      method: "POST",
      body: stringifyContractJson(input),
      headers: { "X-Request-ID": requestId },
      signal,
    });
  }

  async uploadArtifactJobInput(
    jobId: string,
    file: ArtifactJobInputFile,
    content: Blob,
    signal?: AbortSignal,
    onProgress?: (progress: ArtifactTransferProgress) => void,
  ): Promise<ArtifactJob> {
    const path = `/api/artifact-jobs/${encodeURIComponent(jobId)}/inputs/${encodeURIComponent(file.name)}`;
    const csrf = await this.requiredCsrfToken();
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      const abort = () => request.abort();
      const finish = () => signal?.removeEventListener("abort", abort);
      request.open("PUT", path);
      request.responseType = "text";
      request.withCredentials = true;
      request.setRequestHeader("Accept", "application/json");
      request.setRequestHeader("Content-Type", file.media_type);
      request.setRequestHeader("X-Content-SHA256", file.sha256);
      request.setRequestHeader("X-CSRF-Token", csrf);
      request.upload.onprogress = (event) =>
        onProgress?.({
          loaded: event.loaded,
          total: event.lengthComputable ? event.total : content.size,
        });
      request.onabort = () => {
        finish();
        reject(new DOMException("Artifact upload cancelled", "AbortError"));
      };
      request.onerror = () => {
        finish();
        reject(new ApiError(0, "Artifact upload failed before the controller responded"));
      };
      request.onload = () => {
        finish();
        const response = new Response(null, { status: request.status });
        let value: unknown;
        try {
          value = validateControlBody(
            "PUT",
            path,
            request.status,
            request.getResponseHeader("Content-Type") ?? "",
            request.responseText,
          );
        } catch (cause) {
          try {
            this.requireAuthentication(response, cause);
          } catch (error) {
            reject(error);
            return;
          }
          reject(cause);
          return;
        }
        try {
          this.requireAuthentication(response);
        } catch (error) {
          reject(error);
          return;
        }
        if (request.status < 200 || request.status >= 300) {
          const detail =
            typeof value === "object" && value !== null && "detail" in value
              ? formatApiDetail(value.detail)
              : "request failed";
          reject(
            new ApiError(
              request.status,
              `Control API returned ${request.status}: ${detail}`,
              requestIdOf(request),
            ),
          );
          return;
        }
        // The sole cast is the declared, runtime-validated network DTO boundary.
        resolve(value as ArtifactJob);
      };
      if (signal?.aborted) {
        request.abort();
        return;
      }
      signal?.addEventListener("abort", abort, { once: true });
      request.send(content);
    });
  }

  finalizeArtifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/finalize`, {
      method: "POST",
      signal,
    });
  }

  submitArtifactJob(jobId: string, requestId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/submit`, {
      method: "POST",
      headers: { "X-Request-ID": requestId },
      signal,
    });
  }

  artifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}`, { signal });
  }

  cancelArtifactJob(
    jobId: string,
    reason: string,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ArtifactJob> {
    return this.request(`/api/artifact-jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
      body: stringifyContractJson({ reason }),
      headers: { "X-Request-ID": requestId },
      signal,
    });
  }

  artifactJobResultUrl(jobId: string, name: string, sha256: string): string {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) {
      throw new Error("Unsafe artifact result name");
    }
    if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error("Unsafe artifact result digest");
    const encodedJobId = encodeURIComponent(jobId);
    const encodedName = encodeURIComponent(name);
    return `/api/artifact-jobs/${encodedJobId}/results/${encodedName}/${sha256}`;
  }

  async operations(
    cursor?: string,
    signal?: AbortSignal,
    filters: ActivityFilters = {},
  ): Promise<OperationsResponse> {
    return resultData(
      await this.generated.GET("/api/operations", {
        params: {
          query: {
            cursor,
            limit: 20,
            state: filters.state,
            node_id: filters.target,
            request_id: filters.requestId,
          },
        },
        signal,
      }),
    );
  }

  async operation(operationId: string, signal?: AbortSignal): Promise<OperationDetail> {
    return resultData(
      await this.generated.GET("/api/operations/{operation_id}", {
        params: { path: { operation_id: operationId } },
        signal,
      }),
    );
  }

  async job(jobId: string, operationCursor?: string, targetCursor?: string): Promise<JobDetail> {
    return resultData(
      await this.generated.GET("/api/jobs/{job_id}", {
        params: {
          path: { job_id: jobId },
          query: { limit: 20, operation_cursor: operationCursor, target_cursor: targetCursor },
        },
      }),
    );
  }

  async resumeJob(jobId: string): Promise<JobResumeResponse> {
    return resultData(
      await this.generated.POST("/api/jobs/{job_id}/resume", {
        params: { path: { job_id: jobId } },
      }),
    );
  }
}

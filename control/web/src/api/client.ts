import createClient from "openapi-fetch";
import {AuthenticationRequired} from "../auth";
import type {paths} from "./generated";
import type {
  AuthSession,
  AgentsResponse,
  AuditSummary,
  ControlApi,
  DocumentList,
  EnrollmentDecisionResponse,
  EnrollmentGrantResponse,
  EnrollmentListResponse,
  FleetResponse,
  JobDetail,
  JobResumeResponse,
  JobsResponse,
  ProposalInput,
  ProposalPreview,
  PackageInventory,
  PackagePlan,
  PackageProgress,
  PackageRemovalProgress,
  PackageRemovalPreview,
  ReconciliationAccepted,
  ReconciliationPlan,
  UpdatePlan,
  UpdateRollout,
  UpdateSkew,
  CatalogRecipeDocument,
  CatalogRecipeList,
  CatalogRecipeRevision,
  GlobalRecipeRevision,
  WorkloadRunApplied,
  WorkloadRunPreview,
  SourceBundleReceipt,
  SourcePolicyReport,
  RecipeBuildPlan,
  RecipeMappingPlan,
  RecipeOperation,
} from "./types";
import type {
  PackageCandidate,
  PackageCandidateSummary,
  PackageDeployment,
  PackageFamily,
  PackagePreview,
  PackageRollout,
  PackageRolloutPreview,
} from "../pages/package-types";

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

function resultData<T>(result: {data?: T; error?: unknown; response: Response}): T {
  if (result.data === undefined) {
    const detail = typeof result.error === "object" && result.error !== null && "detail" in result.error
      ? String(result.error.detail).slice(0, 256)
      : "request failed";
    throw new Error(`Control API returned ${result.response.status}: ${detail}`);
  }
  return result.data;
}

function requireBoundUpdateTarget(value: unknown): void {
  if (typeof value !== "object" || value === null || !("target" in value)) {
    throw new Error("Control API update target identity is invalid");
  }
  const target = value.target;
  if (typeof target !== "object" || target === null) {
    throw new Error("Control API update target identity is invalid");
  }
  const document = target as Record<string, unknown>;
  const targetSha = document.target_sha256;
  const releaseDigest = document.release_digest;
  const release = document.release;
  const platformVersion = document.platform_version;
  if (
    typeof targetSha !== "string"
    || !/^[0-9a-f]{64}$/.test(targetSha)
    || releaseDigest !== `sha256:${targetSha}`
    || typeof platformVersion !== "string"
    || typeof release !== "string"
    || release !== `platform/releases/${platformVersion}/${targetSha}.json`
  ) {
    throw new Error("Control API update target identity is invalid");
  }
}

function packagePreview(value: PackagePlan): PackagePreview {
  return {
    digest: value.digest,
    release_digest: value.release_digest ?? undefined,
  };
}

function packageCandidateSummary(value: {
  id: string;
  family_id: string;
  state: string;
  reason_code?: string | null;
  upstream_version: string;
}): PackageCandidateSummary {
  return {
    id: value.id,
    family_id: value.family_id,
    channel: null,
    provider: null,
    state: value.state,
    reason_code: value.reason_code ?? null,
    upstream_version: value.upstream_version,
    updated_at: null,
  };
}

type PackageCandidateDocument = {
  id: string;
  family_id: string;
  state: string;
  reason_code?: string | null;
  upstream_version: string;
  release?: {
    lock_digest: string;
    components?: Array<{name: string}>;
    dependencies?: string[];
    provenance?: Array<{kind: string}>;
  } | null;
};

function packageCandidate(value: PackageCandidateDocument): PackageCandidate {
  const release = value.release;
  return {
    ...packageCandidateSummary(value),
    lock: release ? {
      digest: release.lock_digest,
      components: (release.components ?? []).map(component => component.name),
      dependencies: release.dependencies ?? [],
      provenance: (release.provenance ?? []).map(item => item.kind).join(", ") || "—",
    } : null,
    compatibility: undefined,
    validations: [],
    audit: [],
  };
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

  previewRecipeMapping(recipeRevisionId: string, profileName: string, nodeIds: string[]): Promise<RecipeMappingPlan> {
    return this.request("/api/v1/recipes/mapping-plans/preview", {method: "POST", body: JSON.stringify({recipe_revision_id: recipeRevisionId, profile_name: profileName, node_ids: nodeIds, parameters: {}})});
  }

  createRecipeMapping(plan: RecipeMappingPlan): Promise<{mapping_id: string; generation: number; placement_digest: string}> {
    return this.request("/api/v1/recipes/mappings", {method: "POST", body: JSON.stringify({recipe_revision_id: plan.recipe_revision_id, profile_name: plan.profile_name, node_ids: plan.nodes.map(node => node.node_id), parameters: plan.parameters, placement_digest: plan.placement_digest, request_key: crypto.randomUUID()})});
  }

  previewWorkloadRun(sourceYaml: string): Promise<WorkloadRunPreview> {
    return this.request("/api/v1/catalog/imports/workload_run/preview", {method: "POST", body: JSON.stringify({source_yaml: sourceYaml})});
  }

  applyWorkloadRun(sourceYaml: string, sourceSha256: string, reportDigest: string): Promise<WorkloadRunApplied> {
    return this.request("/api/v1/catalog/imports/workload_run", {method: "POST", body: JSON.stringify({source_yaml: sourceYaml, source_sha256: sourceSha256, report_digest: reportDigest})});
  }

  async fleet(): Promise<FleetResponse> {
    return resultData(await this.generated.GET("/api/v1/fleet"));
  }

  async agents(): Promise<AgentsResponse> {
    return resultData(await this.generated.GET("/api/v1/agents"));
  }

  async enrollments(): Promise<EnrollmentListResponse> {
    return resultData(await this.generated.GET("/api/v1/agents/enrollments"));
  }

  async createEnrollmentGrant(nodeId: string, ttlSeconds: number, signal?: AbortSignal): Promise<EnrollmentGrantResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/enrollments/grants", {
      body: {node_id: nodeId, ttl_seconds: ttlSeconds},
      signal,
    }));
  }

  async createAgentMigrationGrant(nodeId: string, ttlSeconds: number, signal?: AbortSignal): Promise<EnrollmentGrantResponse> {
    return resultData(await this.generated.POST("/api/v1/agents/nodes/{node_id}/migration-grant", {
      body: {ttl_seconds: ttlSeconds},
      params: {path: {node_id: nodeId}},
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

  async planProfile(profileId: string): Promise<ReconciliationPlan> {
    return resultData(await this.generated.POST("/api/v1/profiles/{profile_id}/plan", {
      params: {path: {profile_id: profileId}},
    }));
  }

  async applyReconciliation(digest: string, fleetEvidenceDigest: string): Promise<ReconciliationAccepted> {
    return resultData(await this.generated.POST("/api/v1/reconciliations", {
      body: {plan_digest: digest, fleet_evidence_digest: fleetEvidenceDigest},
    }));
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

  documents(kind: "models" | "profiles") { return this.request<DocumentList>(`/api/v1/documents?kind=${kind}`); }
  audit() { return this.request<{events: AuditSummary[]}>("/api/v1/audit"); }
  preview(input: ProposalInput) { return this.request<ProposalPreview>("/api/v1/proposals", {method: "POST", body: JSON.stringify(input)}); }
  submit(digest: string) { return this.request<Record<string, unknown>>("/api/v1/changes", {method: "POST", body: JSON.stringify({proposal_digest: digest})}); }
  async updateSkew() {
    const result = await this.request<UpdateSkew>("/api/v1/updates/skew");
    requireBoundUpdateTarget(result);
    return result;
  }
  async planUpdate(release: string) {
    const result = await this.request<UpdatePlan>("/api/v1/updates/plan", {
      method: "POST", body: JSON.stringify({release}),
    });
    requireBoundUpdateTarget(result);
    return result;
  }
  applyUpdate(planDigest: string) {
    return this.request<UpdateRollout>("/api/v1/updates", {
      method: "POST", body: JSON.stringify({plan_digest: planDigest}),
    });
  }
  updateStatus(rolloutId: string) {
    return this.request<UpdateRollout>(`/api/v1/updates/${encodeURIComponent(rolloutId)}`);
  }
  approveUpdateResume(rolloutId: string) {
    return this.request<UpdateRollout>(`/api/v1/updates/${encodeURIComponent(rolloutId)}/approve-resume`, {
      method: "POST", body: JSON.stringify({}),
    });
  }

  async packageFamilies(): Promise<PackageFamily[]> {
    const result = resultData(await this.generated.GET("/api/v1/packages/families", {
      params: {query: {limit: 100}},
    }));
    return result.families.map(family => ({
      id: family.id,
      channels: family.channels,
      channel: family.channels.join(", "),
      promotion_mode: family.promotion_mode,
    }));
  }

  async packageCandidates(): Promise<PackageCandidateSummary[]> {
    const result = resultData(await this.generated.GET("/api/v1/packages/candidates", {
      params: {query: {limit: 100}},
    }));
    return result.candidates.map(packageCandidateSummary);
  }

  private async candidateDocument(candidateId: string): Promise<PackageCandidateDocument> {
    return resultData(await this.generated.GET("/api/v1/packages/candidates/{candidate_id}", {
      params: {path: {candidate_id: candidateId}},
    }));
  }

  async packageCandidate(candidateId: string): Promise<PackageCandidate> {
    return packageCandidate(await this.candidateDocument(candidateId));
  }

  async previewPackageValidation(candidateId: string): Promise<PackagePreview> {
    return packagePreview(resultData(await this.generated.POST("/api/v1/packages/candidates/{candidate_id}/validation-preview", {
      params: {path: {candidate_id: candidateId}},
    })));
  }

  async validatePackage(candidateId: string, previewDigest: string): Promise<PackageProgress> {
    return resultData(await this.generated.POST("/api/v1/packages/candidates/{candidate_id}/validate", {
      params: {path: {candidate_id: candidateId}},
      body: {plan_digest: previewDigest},
    }));
  }

  async packageValidation(validationId: string): Promise<PackageProgress> {
    return resultData(await this.generated.GET("/api/v1/packages/validations/{validation_id}", {
      params: {path: {validation_id: validationId}},
    }));
  }

  async previewPackagePromotion(candidateId: string): Promise<PackagePreview> {
    return packagePreview(resultData(await this.generated.POST("/api/v1/packages/candidates/{candidate_id}/promotion-preview", {
      params: {path: {candidate_id: candidateId}},
    })));
  }

  async promotePackage(candidateId: string, previewDigest: string): Promise<{release_digest: string}> {
    const result = resultData(await this.generated.POST("/api/v1/packages/candidates/{candidate_id}/promote", {
      params: {path: {candidate_id: candidateId}},
      body: {preview_digest: previewDigest},
    }));
    return {release_digest: result.release_digest};
  }

  async deployments(): Promise<PackageDeployment[]> {
    const result = resultData(await this.generated.GET("/api/v1/deployments", {
      params: {query: {limit: 100}},
    }));
    return result.deployments.map(deployment => ({
      id: deployment.id,
      family_id: deployment.family_id ?? "",
      release_digest: deployment.release_digest,
      previous_release_digest: deployment.previous_release_digest ?? null,
      state: deployment.state,
      rollout_id: deployment.rollout_id ?? null,
    }));
  }

  async previewPackageRollout(deploymentId: string): Promise<PackageRolloutPreview> {
    const value = resultData(await this.generated.POST("/api/v1/deployments/{deployment_id}/rollout-preview", {
      params: {path: {deployment_id: deploymentId}},
    }));
    return {
      ...packagePreview(value),
      canary: value.canary_node ? [value.canary_node] : [],
      batches: value.batches ?? [],
      offline_pending: value.offline_pending ?? [],
      download_remaining_bytes: value.download_bytes ?? 0,
      storage_required_bytes: value.storage_bytes ?? 0,
    };
  }

  async startPackageRollout(deploymentId: string, previewDigest: string): Promise<{id: string; plan_digest: string}> {
    const result = resultData(await this.generated.POST("/api/v1/deployments/{deployment_id}/rollouts", {
      params: {path: {deployment_id: deploymentId}},
      body: {plan_digest: previewDigest},
    }));
    return {id: result.id, plan_digest: result.plan_digest};
  }

  async packageRollout(deploymentId: string, rolloutId: string): Promise<PackageRollout> {
    const result = resultData(await this.generated.GET("/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}", {
      params: {path: {deployment_id: deploymentId, rollout_id: rolloutId}, query: {limit: 100}},
    }));
    return {
      id: result.id,
      state: result.state,
      phase: result.state,
      failure_reason: result.failure ?? null,
      nodes: (result.nodes ?? []).map(node => ({name: node.node_id, state: node.state})),
    };
  }

  async previewPackageRollback(deploymentId: string, rolloutId: string): Promise<PackagePreview> {
    return packagePreview(resultData(await this.generated.POST("/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}/rollback-preview", {
      params: {path: {deployment_id: deploymentId, rollout_id: rolloutId}},
    })));
  }

  async rollbackPackage(deploymentId: string, rolloutId: string, previewDigest: string): Promise<{id: string}> {
    const result = resultData(await this.generated.POST("/api/v1/deployments/{deployment_id}/rollouts/{rollout_id}/rollback", {
      params: {path: {deployment_id: deploymentId, rollout_id: rolloutId}},
      body: {plan_digest: previewDigest},
    }));
    return {id: result.id};
  }

  async packageInventory(nodeId?: string, deploymentId?: string, cursor?: string): Promise<PackageInventory> {
    return resultData(await this.generated.GET("/api/v1/packages/inventory", {
      params: {query: {node_id: nodeId, deployment_id: deploymentId, cursor, limit: 100}},
    }));
  }

  async previewPackageRemoval(input: {deployment_id: string; release_digest: string; node_ids: string[]}): Promise<PackageRemovalPreview> {
    return resultData(await this.generated.POST("/api/v1/packages/inventory/remove-preview", {
      body: input,
    }));
  }

  async removePackageInventory(planDigest: string): Promise<PackageRemovalProgress> {
    return resultData(await this.generated.POST("/api/v1/packages/inventory/remove", {
      body: {plan_digest: planDigest},
    }));
  }

  async previewPackageGc(): Promise<PackagePlan> {
    return resultData(await this.generated.POST("/api/v1/packages/gc-preview"));
  }

  async applyPackageGc(planDigest: string): Promise<PackageProgress> {
    return resultData(await this.generated.POST("/api/v1/packages/gc", {
      body: {plan_digest: planDigest},
    }));
  }
}

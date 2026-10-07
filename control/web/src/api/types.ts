import type { FleetEventStream } from "./fleet-event-connection";
import type { WireNumber } from "./contract-numeric";
import type { components, paths } from "./generated";

export type AuthSession = components["schemas"]["AuthSession"];
export type AvailabilityOperationFailure = components["schemas"]["AvailabilityOperationFailure"];
export type CliTokenDownload = components["schemas"]["CliTokenDownload"];
export type TelemetryPoint = components["schemas"]["TelemetryPoint"];
export type FleetTelemetryState = components["schemas"]["TelemetryState"];
export type VisualFleetNode = components["schemas"]["FleetNode"];
export type VisualFleetSnapshot = components["schemas"]["FleetSnapshot"];
export type EnrollmentGrantResponse = components["schemas"]["EnrollmentGrantResponse"];
export type FleetActionResponse = components["schemas"]["FleetActionResponse"];
export type FleetEnrollRequest = components["schemas"]["FleetEnrollRequest"];
export type FleetNodeIdentity = components["schemas"]["FleetNodeIdentity"];
export type FleetLogResponse = components["schemas"]["FleetLogResponse"];
export type EnrollmentGrantStatus = components["schemas"]["EnrollmentGrantStatus"];
export type GatewayKeyList = components["schemas"]["GatewayKeyList"];
export type GatewayKeyCreated = components["schemas"]["GatewayKeyCreated"];
export type GatewayKeyRevoked = components["schemas"]["GatewayKeyRevoked"];
export type JobDetail = components["schemas"]["JobDetailResponse"];
export type JobResumeResponse = components["schemas"]["JobResumeResponse"];
export type OperationDetail = components["schemas"]["OperationDetailResponse"];
export type CatalogSyncStatus = components["schemas"]["ManagedCatalogSyncResponse"];
export type ProfileDefinition = components["schemas"]["FleetProfileDefinitionView"];
export type ReconcilePlan = components["schemas"]["RunSwitchPlan"];
export type ActivityFilters = { state?: string; target?: string; requestId?: string };
export type OperationsResponse = components["schemas"]["OperationsResponse"];
export type OperationBlocker = components["schemas"]["OperationBlocker"];
export type ModelDefinition = components["schemas"]["ModelDefinition"];
export type RecipeDefinition = components["schemas"]["RecipeDefinition"];
export type ModelLibrary = components["schemas"]["ModelLibraryResponse"];
export type RecipeLibrary = components["schemas"]["RecipeLibraryResponse"];
export type RecipeDetail = components["schemas"]["RecipeDetailResponse"];
export type RecipeAlternative = components["schemas"]["RecipeAlternative"];
export type CacheRemovalReview = components["schemas"]["CacheRemovalReview"];
export type ModelCacheOperatorResponse = components["schemas"]["ModelCacheOperatorResponse"];
export type RecipeImageAvailabilityResponse =
  components["schemas"]["RecipeImageAvailabilityResponse"];
export type RecipeOperatorResponse = components["schemas"]["RecipeOperatorResponse"];
export type RecipeUpdateResponse = components["schemas"]["RecipeUpdateResponse"];
export type RecipeRemovalUnavailable = components["schemas"]["RecipeRemovalUnavailableView"];
export type RecipeCacheOperation =
  | RecipeImageAvailabilityResponse
  | RecipeOperatorResponse
  | RecipeUpdateResponse
  | RecipeRemovalUnavailable;
// The library ordering vocabulary is the generated query parameter, so the UI
// cannot offer a `sort` the API would reject as a 422.
export type LibrarySort = NonNullable<
  NonNullable<paths["/api/model/library"]["get"]["parameters"]["query"]>["sort"]
>;
export type LibraryViewRecipeModel = components["schemas"]["LibraryRecipeModel"];

// UI-only projections combine the independent Model and Recipe list responses.
// They are never sent to the operator API and keep the existing Library layout
// mechanical while the wire authority remains the four current nouns.
export type LibraryViewRecipe = {
  capabilities: string[];
  content_sha256: string;
  description: string;
  recipe_document: RecipeDefinition;
  recipe_id: string;
  recipe_revision_id: string;
  publisher: string;
  slug: string;
  title: string;
  topology_name: string;
  // Projected by the Controller; the same names as vonkctl --engine/--creator.
  engine?: string;
  creator?: string | null;
  model_selectors?: string[];
  installations?: unknown[];
  runs?: unknown[];
  installation_returned_count?: WireNumber;
  installation_total_count?: WireNumber;
  installations_truncated?: boolean;
  run_returned_count?: WireNumber;
  run_total_count?: WireNumber;
  runs_truncated?: boolean;
  reasons?: { code: string; severity: string; detail: string }[];
};
export function canonicalRecipeSelector(
  recipe: Pick<LibraryViewRecipe, "publisher" | "slug">,
): string {
  return `${recipe.publisher}/${recipe.slug}`;
}
export type LibraryViewModel = {
  page_local?: boolean;
  model: { kind: "model"; publisher: string; slug: string; content_sha256: string };
  model_document: ModelDefinition;
  model_capabilities?: string[];
  local: components["schemas"]["LibraryLocalState"];
  // Projected facets, named exactly as the CLI filter flags and the
  // /api/model/library query parameters: --usage --family --version
  // --quantization.
  family: string;
  version: string;
  quantization: string;
  usage: string[];
  // Alignments declared by the recipes that serve this model.
  alignment: string[];
  recipes: LibraryViewRecipe[];
};
export type LibraryViewSnapshot = {
  generated_at: string;
  freshness_policy: components["schemas"]["FreshnessPolicy"];
  library?: components["schemas"]["LibraryRelease"] | null;
  models: LibraryViewModel[];
  unlinked_recipes: LibraryViewRecipe[];
};
export type LibraryViewRecipeDetail = {
  generated_at: string;
  definition: RecipeDefinition;
  recipe: LibraryViewRecipe;
  model_documents: LibraryViewRecipeModel[];
  operational_state: {
    builds: unknown[];
    installations: unknown[];
    mappings: unknown[];
    runs: unknown[];
  };
  placement: {
    recommendations: LibraryViewPlacementGroup[];
    rejected_groups: LibraryViewPlacementGroup[];
    search_complete: boolean;
  }[];
  reasons: { code: string; severity: string; detail: string }[];
  topology: RecipeDefinition["topology"];
  // Other recipes for the same model, excluding this one.
  alternatives?: RecipeAlternative[];
};
export type LibraryViewPlacementGroup = {
  eligible: boolean;
  node_ids: string[];
  nodes: { node_id: string; memory_free_after_bytes: WireNumber }[];
  topology_name: string;
  load_state: string;
  install_state: string;
};
export type ArtifactJobInterface = components["schemas"]["ArtifactJobResponse"]["interface"];
export type ArtifactJobFile = components["schemas"]["ArtifactOutputFile"];
export type ArtifactJobInputFile = components["schemas"]["ArtifactFileDeclaration"];
export type ArtifactJobOutputLimits = components["schemas"]["OutputLimits"];
export type ArtifactJobCreateInput = components["schemas"]["ArtifactJobCreate"];
export type ArtifactJob = components["schemas"]["ArtifactJobResponse"];
export type ArtifactJobList = components["schemas"]["ArtifactJobListResponse"];
export type ArtifactJobCapabilities = components["schemas"]["ArtifactJobCapabilitiesResponse"];
export type ArtifactTransferProgress = { loaded: number; total: number };
export type FleetProfile = components["schemas"]["FleetProfileView"];
export type FleetProfileNumber = FleetProfile["number"];
export type FleetProfileRead = FleetProfile | components["schemas"]["UnavailableFleetProfileView"];
export function readableProfile(profile: FleetProfileRead): profile is FleetProfile {
  return !("projection_issue" in profile);
}
export type FleetProfileInput = components["schemas"]["FleetProfileInput"];
export type FleetProfileList = components["schemas"]["FleetProfileList"];
export type FleetProfilePreview = components["schemas"]["FleetProfilePreview"];
export type FleetProfileEndpoints = components["schemas"]["FleetProfileEndpointsView"];
export type FleetProfileApplicationView = components["schemas"]["FleetProfileApplicationView"];
export type FleetProfileLoadInput = components["schemas"]["FleetProfileLoadRequest"];
export type FleetRefreshEvent = components["schemas"]["FleetRefreshEvent"];
export type FleetTelemetryEvent = components["schemas"]["FleetTelemetryEvent"];
export type FleetChangeEvent = components["schemas"]["FleetChangeEvent"];
export type FleetStreamEvent = components["schemas"]["FleetStreamEvent"];
export interface LibraryApi {
  catalogSyncStatus(signal?: AbortSignal): Promise<CatalogSyncStatus | null>;
  modelLibrary(
    cursor?: string,
    sort?: LibrarySort,
    updatedSince?: string,
    signal?: AbortSignal,
  ): Promise<ModelLibrary>;
  recipeLibrary(
    cursor?: string,
    sort?: LibrarySort,
    updatedSince?: string,
    signal?: AbortSignal,
  ): Promise<RecipeLibrary>;
  recipeDetail(selector: string, signal?: AbortSignal): Promise<RecipeDetail>;
  artifactJobsForRun(runId: string, signal?: AbortSignal): Promise<ArtifactJobList>;
  artifactJobByRequestId(requestId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  artifactJobCapabilities(signal?: AbortSignal): Promise<ArtifactJobCapabilities>;
  createArtifactJob(
    runId: string,
    input: ArtifactJobCreateInput,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ArtifactJob>;
  uploadArtifactJobInput(
    jobId: string,
    file: ArtifactJobInputFile,
    content: Blob,
    signal?: AbortSignal,
    onProgress?: (progress: ArtifactTransferProgress) => void,
  ): Promise<ArtifactJob>;
  finalizeArtifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  submitArtifactJob(jobId: string, requestId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  artifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  cancelArtifactJob(
    jobId: string,
    reason: string,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<ArtifactJob>;
  artifactJobResultUrl(jobId: string, name: string, sha256: string): string;
  prepareModelCache(
    selector: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse>;
  modelRemovalReview(selector: string, signal?: AbortSignal): Promise<CacheRemovalReview>;
  removeModelCache(
    selector: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse>;
  modelCacheRequest(requestKey: string, signal?: AbortSignal): Promise<ModelCacheOperatorResponse>;
  modelCacheOperation(
    operationId: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse>;
  downloadRecipe(
    selector: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeImageAvailabilityResponse>;
  recipeRemovalReview(
    selector: string,
    withModel: boolean,
    signal?: AbortSignal,
  ): Promise<CacheRemovalReview>;
  removeRecipe(
    selector: string,
    requestKey: string,
    withModel: boolean,
    signal?: AbortSignal,
  ): Promise<RecipeOperatorResponse>;
  recipeCacheRequest(requestKey: string, signal?: AbortSignal): Promise<RecipeCacheOperation>;
  updateRecipes(
    all: boolean,
    selectors: string[],
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeUpdateResponse>;
  recipeCacheOperation(operationId: string, signal?: AbortSignal): Promise<RecipeCacheOperation>;
  cancelModelOperation(
    operationId: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<ModelCacheOperatorResponse>;
  cancelRecipeOperation(
    operationId: string,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<RecipeCacheOperation>;
  previewInstallationReconcile(
    installationId: string,
    signal?: AbortSignal,
  ): Promise<ReconcilePlan>;
  reconcileInstallation(
    installationId: string,
    requestKey: string,
    planDigest: string,
    signal?: AbortSignal,
  ): Promise<unknown>;
}
export interface ControlApi extends LibraryApi {
  downloadCliToken(): Promise<CliTokenDownload>;
  profiles(signal?: AbortSignal): Promise<FleetProfileList>;
  profile(number: FleetProfileNumber, signal?: AbortSignal): Promise<FleetProfileRead>;
  autosaveProfile(
    number: FleetProfileNumber,
    input: FleetProfileInput,
    signal?: AbortSignal,
  ): Promise<FleetProfile>;
  previewProfile(number: FleetProfileNumber, signal?: AbortSignal): Promise<FleetProfilePreview>;
  loadProfile(
    number: FleetProfileNumber,
    input: FleetProfileLoadInput,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView>;
  profileApplicationByRequest(
    number: FleetProfileNumber,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView>;
  profileProgress(
    number: FleetProfileNumber,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView>;
  profileEndpoints(
    number: FleetProfileNumber,
    signal?: AbortSignal,
  ): Promise<FleetProfileEndpoints>;
  profileDefinition(number: FleetProfileNumber, signal?: AbortSignal): Promise<ProfileDefinition>;
  cancelProfileApplication(
    applicationId: string,
    profileNumber: FleetProfileNumber,
    requestKey: string,
    signal?: AbortSignal,
  ): Promise<FleetProfileApplicationView>;
  fleetEvents(appliedCursor: () => string): FleetEventStream;
  visualFleet(signal?: AbortSignal): Promise<VisualFleetSnapshot>;
  enrollFleetNode(
    input: FleetEnrollRequest,
    signal?: AbortSignal,
  ): Promise<components["schemas"]["FleetActionResponse"]>;
  fleetNode(selector: string, signal?: AbortSignal): Promise<VisualFleetNode>;
  renameFleetNode(selector: string, displayName: string): Promise<FleetNodeIdentity>;
  removeFleetNode(selector: string): Promise<FleetActionResponse>;
  reenrollFleetNode(selector: string, requestKey: string): Promise<FleetActionResponse>;
  fleetLogs(selector: string, signal?: AbortSignal): Promise<FleetLogResponse>;
  upgradeFleet(all: boolean, selectors: string[], requestKey: string): Promise<FleetActionResponse>;
  enrollmentStatus(grantId: string, signal?: AbortSignal): Promise<EnrollmentGrantStatus>;
  revokeEnrollment(grantId: string): Promise<EnrollmentGrantStatus>;
  gatewayKeys(signal?: AbortSignal): Promise<GatewayKeyList>;
  createGatewayKey(name: string, models: string[], expires?: string): Promise<GatewayKeyCreated>;
  rollGatewayKey(name: string): Promise<GatewayKeyCreated>;
  revokeGatewayKey(name: string): Promise<GatewayKeyRevoked>;
  operations(
    cursor?: string,
    signal?: AbortSignal,
    filters?: ActivityFilters,
  ): Promise<OperationsResponse>;
  operation(operationId: string, signal?: AbortSignal): Promise<OperationDetail>;
  job(jobId: string, operationCursor?: string, targetCursor?: string): Promise<JobDetail>;
  resumeJob(jobId: string): Promise<JobResumeResponse>;
}

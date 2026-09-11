import type {components} from "./generated";

export type AuthSession = components["schemas"]["AuthSession"];
export type AvailabilityOperationFailure = components["schemas"]["AvailabilityOperationFailure"];
export type CliTokenDownload = {expiresAt: string};
export type FleetTelemetryDetails = components["schemas"]["TelemetryPoint"]["details"];
export type TelemetryPoint = components["schemas"]["TelemetryPoint"];
export type FleetTelemetryState = components["schemas"]["TelemetryState"];
export type VisualFleetNode = components["schemas"]["FleetNode"];
export type VisualFleetSnapshot = components["schemas"]["FleetSnapshot"];
export type TelemetryHistory = components["schemas"]["TelemetryHistoryResponse"];
export type TelemetryResolution = TelemetryHistory["resolution"];
export type TelemetryHistoryPoint = components["schemas"]["TelemetryPoint"] | components["schemas"]["TelemetryRollupPoint"];
export type TelemetryHistoryMetadata = components["schemas"]["TelemetryHistoryMetadata"];
export type TelemetryRollupPoint = components["schemas"]["TelemetryRollupPoint"];
export type TelemetryMetricSummary = components["schemas"]["TelemetryMetricSummary"];
export type EnrollmentGrantResponse = components["schemas"]["EnrollmentGrantResponse"];
export type JobDetail = components["schemas"]["JobDetailResponse"];
export type JobResumeResponse = components["schemas"]["JobResumeResponse"];
export type JobSummary = components["schemas"]["JobSummary"];
export type JobsResponse = components["schemas"]["JobsResponse"];
export type OperationDetail = components["schemas"]["OperationDetailResponse"];
export type OperationsResponse = components["schemas"]["OperationsResponse"];
export type AuditSummary = components["schemas"]["AuditEventResponse"];
export type AuditResponse = components["schemas"]["AuditResponse"];
export type ModelDefinition = components["schemas"]["ModelDefinition"];
export type RecipeDefinition = components["schemas"]["RecipeDefinition"];
export type ModelStatus = components["schemas"]["ModelLibraryResponse"];
export type ModelLibrary = components["schemas"]["ModelLibraryResponse"];
export type ModelDetail = components["schemas"]["ModelDetailResponse"];
export type RecipeStatus = components["schemas"]["RecipeLibraryResponse"];
export type RecipeLibrary = components["schemas"]["RecipeLibraryResponse"];
export type RecipeDetail = components["schemas"]["RecipeDetailResponse"];
export type ModelCacheOperatorResponse = components["schemas"]["ModelCacheOperatorResponse"];
export type RecipeImageAvailabilityResponse = components["schemas"]["RecipeImageAvailabilityResponse"];
export type RecipeOperatorResponse = components["schemas"]["RecipeOperatorResponse"];
export type RecipeCacheOperation = RecipeImageAvailabilityResponse | RecipeOperatorResponse;
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
  model_selectors?: string[];
  installations?: unknown[];
  runs?: unknown[];
  recipe_capabilities?: {facts: {capability: string; support: string}[]; [key: string]: unknown};
  installation_returned_count?: number;
  installation_total_count?: number;
  installations_truncated?: boolean;
  run_returned_count?: number;
  run_total_count?: number;
  runs_truncated?: boolean;
  reasons?: {code: string; severity: string; detail: string}[];
};
export function canonicalRecipeSelector(recipe: Pick<LibraryViewRecipe, "publisher" | "slug">): string {
  return `${recipe.publisher}/${recipe.slug}`;
}
export type LibraryViewModel = {
  page_local?: boolean;
  model: {kind: "model"; publisher: string; slug: string; content_sha256: string};
  model_document: ModelDefinition;
  model_capabilities?: {facts: {capability: string; support: string}[]; [key: string]: unknown};
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
  schema_version: 2;
  generated_at: string;
  freshness_policy: components["schemas"]["FreshnessPolicy"];
  models: LibraryViewModel[];
  unlinked_recipes: LibraryViewRecipe[];
};
export type LibraryViewRecipeDetail = {
  schema_version: 2;
  generated_at: string;
  definition: RecipeDefinition;
  recipe: LibraryViewRecipe;
  model_documents: LibraryViewRecipeModel[];
  model_capabilities?: {facts: {capability: string; support: string}[]; [key: string]: unknown};
  recipe_capabilities?: {facts: {capability: string; support: string}[]; [key: string]: unknown};
  operational_state: {builds: unknown[]; installations: unknown[]; mappings: unknown[]; runs: unknown[]};
  placement: {recommendations: LibraryViewPlacementGroup[]; rejected_groups: LibraryViewPlacementGroup[]; search_complete: boolean}[];
  reasons: {code: string; severity: string; detail: string}[];
  topology: RecipeDefinition["topology"];
};
export type LibraryViewPlacementGroup = {eligible: boolean; node_ids: string[]; nodes: {node_id: string; memory_free_after_bytes: number}[]; topology_name: string; load_state: string; install_state: string};
export type ArtifactJobInterface = components["schemas"]["ArtifactJobResponse"]["interface"];
export type ArtifactJobFile = components["schemas"]["ArtifactOutputFile"];
export type ArtifactJobInputFile = components["schemas"]["ArtifactFileDeclaration"];
export type ArtifactJobOutputLimits = components["schemas"]["OutputLimits"];
export type ArtifactJobCreateInput = components["schemas"]["ArtifactJobCreate"];
export type ArtifactJob = components["schemas"]["ArtifactJobResponse"];
export type ArtifactJobList = components["schemas"]["ArtifactJobListResponse"];
export type ArtifactJobCapabilities = components["schemas"]["ArtifactJobCapabilitiesResponse"];
export type ArtifactTransferProgress = {loaded: number; total: number};
export type FleetProfile = components["schemas"]["FleetProfileView"];
export type FleetProfileInput = components["schemas"]["FleetProfileInput"];
export type FleetProfileList = components["schemas"]["FleetProfileList"];
export type FleetProfilePreview = components["schemas"]["FleetProfilePreview"];
export type FleetProfileApplicationView = components["schemas"]["FleetProfileApplicationView"];
export type FleetProfileLoadInput = components["schemas"]["FleetProfileLoadRequest"];
export type TelemetryScope = components["schemas"]["TelemetrySeries"]["scope"];
export type TelemetrySupport = components["schemas"]["TelemetrySeries"]["support_status"];
export type TelemetryFreshness = components["schemas"]["TelemetrySeries"]["freshness"];
export type TelemetryMeasurementKind = components["schemas"]["TelemetrySeries"]["measurement_kind"];
export type TelemetrySeries = components["schemas"]["TelemetrySeries"];
export type TelemetryCapability = components["schemas"]["TelemetryCapability"];
export type TelemetryProvenance = components["schemas"]["TelemetryProvenance"];
export type TelemetryRuntime = components["schemas"]["TelemetryRuntime"];
export type TelemetryWorkload = components["schemas"]["TelemetryWorkload"];
export type TelemetryMetrics = components["schemas"]["TelemetryMetrics"];
export type RichTelemetryPoint = TelemetryPoint;
export type FleetSnapshotEvent = components["schemas"]["FleetSnapshotEvent"];
export type FleetTelemetryEvent = components["schemas"]["FleetTelemetryEvent"];
export type FleetChangeEvent = components["schemas"]["FleetChangeEvent"];
export type FleetStreamEvent = components["schemas"]["FleetStreamEvent"];
export interface CatalogApi {
}
export interface LibraryApi {
  modelStatus(signal?: AbortSignal): Promise<ModelStatus>;
  modelLibrary(cursor?: string, signal?: AbortSignal): Promise<ModelLibrary>;
  modelDetail(selector: string, signal?: AbortSignal): Promise<ModelDetail>;
  recipeStatus(signal?: AbortSignal): Promise<RecipeStatus>;
  recipeLibrary(cursor?: string, signal?: AbortSignal): Promise<RecipeLibrary>;
  recipeDetail(selector: string, signal?: AbortSignal): Promise<RecipeDetail>;
  libraryJobProgress(jobId: string, signal?: AbortSignal): Promise<JobDetail>;
  artifactJobsForRun(runId: string, signal?: AbortSignal): Promise<ArtifactJobList>;
  artifactJobCapabilities(signal?: AbortSignal): Promise<ArtifactJobCapabilities>;
  createArtifactJob(runId: string, input: ArtifactJobCreateInput, signal?: AbortSignal): Promise<ArtifactJob>;
  uploadArtifactJobInput(jobId: string, file: ArtifactJobInputFile, content: Blob, signal?: AbortSignal, onProgress?: (progress: ArtifactTransferProgress) => void): Promise<ArtifactJob>;
  finalizeArtifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  submitArtifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  artifactJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  cancelArtifactJob(jobId: string, reason: string, signal?: AbortSignal): Promise<ArtifactJob>;
  artifactJobResult(jobId: string, signal?: AbortSignal): Promise<ArtifactJob>;
  artifactJobResultUrl(jobId: string, sha256: string): string;
  prepareModelCache(selector: string, requestKey: string, signal?: AbortSignal): Promise<ModelCacheOperatorResponse>;
  removeModelCache(selector: string, requestKey: string, signal?: AbortSignal): Promise<ModelCacheOperatorResponse>;
  modelCacheOperation(operationId: string, signal?: AbortSignal): Promise<ModelCacheOperatorResponse>;
  downloadRecipe(selector: string, requestKey: string, signal?: AbortSignal): Promise<RecipeImageAvailabilityResponse>;
  removeRecipe(selector: string, requestKey: string, withModel: boolean, signal?: AbortSignal): Promise<RecipeOperatorResponse>;
  recipeCacheOperation(operationId: string, signal?: AbortSignal): Promise<RecipeCacheOperation>;
}
export interface ControlApi extends LibraryApi {
  downloadCliToken(): Promise<CliTokenDownload>;
  profiles(signal?: AbortSignal): Promise<FleetProfileList>;
  profile(number: number, signal?: AbortSignal): Promise<FleetProfile>;
  autosaveProfile(number: number, input: FleetProfileInput, signal?: AbortSignal): Promise<FleetProfile>;
  previewProfile(number: number, signal?: AbortSignal): Promise<FleetProfilePreview>;
  loadProfile(number: number, input?: FleetProfileLoadInput, signal?: AbortSignal): Promise<FleetProfileApplicationView>;
  profileProgress(number: number, signal?: AbortSignal): Promise<FleetProfileApplicationView>;
  visualFleet(signal?: AbortSignal): Promise<VisualFleetSnapshot>;
  jobs(cursor?: string): Promise<JobsResponse>;
  operations(cursor?: string, signal?: AbortSignal): Promise<OperationsResponse>;
  operation(operationId: string, signal?: AbortSignal): Promise<OperationDetail>;
  job(jobId: string, operationCursor?: string, targetCursor?: string): Promise<JobDetail>;
  resumeJob(jobId: string): Promise<JobResumeResponse>;
  audit(signal?: AbortSignal): Promise<AuditResponse>;
}

import {useEffect, useMemo, useRef, useState} from "react";
import type {DragEvent, MouseEvent, ReactNode} from "react";
import type {
  LibraryApi,
  ManagedCatalogSyncSummary,
  LibraryModel,
  LibraryOperation,
  LibraryRecipeDetail,
  LibraryRecipeSummary,
  LibrarySnapshot,
  PublicRecipe,
  PublicRecipeCapability,
  VisualFleetNode,
  VisualFleetSnapshot,
} from "../api/types";
import {formatBytes} from "../lib/fleet";
import type {LibraryRoute} from "../lib/library-route";
import {modelLibraryPath, modelVersionKey, recipeLibraryPath, unlinkedLibraryPath} from "../lib/library-route";
import type {LibraryPlacementGroup} from "./library-action-types";
import {LibraryActionDialog} from "./library-action-dialog";
import {LibraryModelDeletionDialog} from "./library-model-deletion-dialog";
import {LibraryOperationProgress, operationSettled} from "./library-operation-progress";
import {LibraryPlacementDialog} from "./library-placement-dialog";
import type {LibraryPlacementInvocation} from "./library-placement-dialog";
import {friendlyModelName, humanizeIdentifier, TechnicalDetails} from "./library-technical-details";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type SparkFilter = "" | "1" | "2" | "3" | "4+";
type UpdatedFilter = "" | "7" | "30" | "90" | "365";
type LocalFilter = "" | PublicRecipe["local"]["status"] | "needs-review" | "custom" | "withdrawn";
type ModelType = "" | "language" | "vision" | "image" | "video" | "audio" | "3d";

export type ManagedCatalogWithdrawal = ManagedCatalogSyncSummary["withdrawn_recipes"][number];

export type LibraryWorkcellFilters = {
  capabilities: PublicRecipeCapability[];
  local: LocalFilter;
  model: string;
  modelType: ModelType;
  modelVersion: string;
  qualification: "" | PublicRecipe["qualification"];
  quantization: string;
  readiness: "" | PublicRecipe["execution_readiness"];
  repository: string;
  runtime: string;
  sourceOwner: string;
  sparks: SparkFilter;
  topology: string;
  updated: UpdatedFilter;
};

export const EMPTY_LIBRARY_WORKCELL_FILTERS: LibraryWorkcellFilters = {
  capabilities: [], local: "", model: "", modelType: "", modelVersion: "", qualification: "", quantization: "", readiness: "",
  repository: "", runtime: "", sourceOwner: "", sparks: "", topology: "", updated: "",
};

export type LibraryRecipeRecord = {
  catalog?: PublicRecipe;
  custom: boolean;
  key: string;
  managed: boolean;
  model?: LibraryModel["model"];
  modelKey: string;
  modelTitle: string;
  recipe?: LibraryRecipeSummary;
  title: string;
  withdrawal?: ManagedCatalogWithdrawal;
  withdrawnInstalled: boolean;
};

const CAPABILITIES: Array<{label: string; value: PublicRecipeCapability}> = [
  {value: "chat", label: "Chat"},
  {value: "reasoning", label: "Reasoning"},
  {value: "vision", label: "Vision"},
  {value: "image-generation", label: "Image generation"},
  {value: "image-editing", label: "Image editing"},
  {value: "video", label: "Video"},
  {value: "audio", label: "Audio"},
  {value: "3d", label: "3D"},
];

function normalizedCapabilities(recipe: LibraryRecipeSummary): PublicRecipeCapability[] {
  const source = recipe.capabilities.map(value => value.toLowerCase());
  return CAPABILITIES.flatMap(option => {
    const aliases = option.value === "chat" ? ["chat", "openai.chat", "openai.completions"]
      : option.value === "image-generation" ? ["image-generation", "image_generation", "image"]
        : option.value === "image-editing" ? ["image-editing", "image_editing"]
          : [option.value];
    return source.some(value => aliases.some(alias => value.includes(alias))) ? [option.value] : [];
  });
}

export function buildLibraryRecipeRecords(snapshot: LibrarySnapshot, publicRecipes: PublicRecipe[]): LibraryRecipeRecord[] {
  const catalogByLocalId = new Map(publicRecipes.flatMap(recipe => recipe.local.recipe_id ? [[recipe.local.recipe_id, recipe] as const] : []));
  const records: LibraryRecipeRecord[] = [];
  for (const libraryModel of snapshot.models) {
    for (const recipe of libraryModel.recipes) {
      const catalog = catalogByLocalId.get(recipe.recipe_id);
      records.push({
        catalog,
        custom: !catalog,
        key: recipe.recipe_id,
        managed: Boolean(catalog),
        model: libraryModel.model,
        modelKey: modelVersionKey(libraryModel.model),
        modelTitle: catalog?.model_version_title ?? friendlyModelName(libraryModel.model),
        recipe,
        title: recipe.title,
        withdrawnInstalled: false,
      });
    }
  }
  for (const recipe of snapshot.unlinked_recipes) {
    const catalog = catalogByLocalId.get(recipe.recipe_id);
    records.push({catalog, custom: !catalog, key: recipe.recipe_id, managed: Boolean(catalog), modelKey: "unlinked", modelTitle: "Unlinked", recipe, title: recipe.title, withdrawnInstalled: false});
  }
  return records;
}

export function applyManagedCatalogWithdrawals(
  records: LibraryRecipeRecord[],
  fleet: VisualFleetSnapshot | undefined,
  withdrawals: ManagedCatalogWithdrawal[],
): LibraryRecipeRecord[] {
  const withdrawalsByRecipe = new Map(withdrawals.map(item => [item.recipe_id, item]));
  const installedRecipeIds = new Set(fleet?.nodes.flatMap(node => [
    ...(node.installed ?? []).flatMap(item => item.rank_state === "installed" ? [item.recipe_id] : []),
    ...(node.loaded ?? []).map(item => item.recipe_id),
  ]) ?? []);
  return records.map(record => {
    const recipeId = record.recipe?.recipe_id;
    const withdrawal = recipeId ? withdrawalsByRecipe.get(recipeId) : undefined;
    if (!withdrawal) return record;
    return {
      ...record,
      custom: false,
      managed: true,
      withdrawal,
      withdrawnInstalled: installedRecipeIds.has(withdrawal.recipe_id),
    };
  });
}

function recordCapabilities(record: LibraryRecipeRecord): PublicRecipeCapability[] {
  return record.catalog?.capabilities ?? (record.recipe ? normalizedCapabilities(record.recipe) : []);
}

function modelTypeMatches(record: LibraryRecipeRecord, modelType: ModelType): boolean {
  if (!modelType) return true;
  const capabilities = recordCapabilities(record);
  if (modelType === "language") return capabilities.includes("chat") || capabilities.includes("reasoning");
  if (modelType === "image") return capabilities.includes("image-generation") || capabilities.includes("image-editing");
  return capabilities.includes(modelType);
}

function updatedMatches(record: LibraryRecipeRecord, days: UpdatedFilter, now: Date): boolean {
  if (!days) return true;
  if (!record.catalog?.release_released_at) return false;
  const released = new Date(`${record.catalog.release_released_at}T00:00:00Z`);
  return !Number.isNaN(released.getTime()) && released.getTime() >= now.getTime() - Number(days) * 86_400_000;
}

function localMatches(record: LibraryRecipeRecord, local: LocalFilter): boolean {
  if (!local) return true;
  if (local === "custom") return record.custom;
  if (local === "withdrawn") return record.withdrawnInstalled;
  if (!record.catalog) return false;
  if (local === "needs-review") return ["different-revision", "local-ahead", "conflict"].includes(record.catalog.local.status);
  return record.catalog.local.status === local;
}

export function filterLibraryRecipeRecords(records: LibraryRecipeRecord[], filters: LibraryWorkcellFilters, query: string, now = new Date()): LibraryRecipeRecord[] {
  const normalized = query.trim().toLocaleLowerCase();
  return records.filter(record => {
    const catalog = record.catalog;
    const recipe = record.recipe;
    const capabilityValues = recordCapabilities(record);
    const searchable = [record.title, record.modelTitle, recipe?.slug ?? "", recipe?.description ?? "", catalog?.description ?? "", catalog?.source_owner ?? "", catalog?.source_repository ?? "", catalog?.runtime_distribution ?? "", ...(catalog?.quantizations ?? []), ...capabilityValues].join(" ").toLocaleLowerCase();
    const sparks = catalog?.node_count ?? (recipe?.topology_name?.includes("dual") || recipe?.topology_name?.includes("pair") ? 2 : 1);
    return (!normalized || searchable.includes(normalized))
      && modelTypeMatches(record, filters.modelType)
      && (!filters.model || `${catalog?.model_publisher ?? record.model?.publisher ?? ""}/${catalog?.model_slug ?? record.model?.slug ?? ""}` === filters.model)
      && (!filters.modelVersion || record.modelKey === filters.modelVersion)
      && (!filters.sourceOwner || catalog?.source_owner === filters.sourceOwner)
      && (!filters.repository || catalog?.source_repository === filters.repository)
      && (!filters.sparks || (filters.sparks === "4+" ? sparks >= 4 : sparks === Number(filters.sparks)))
      && (!filters.runtime || catalog?.runtime_distribution === filters.runtime)
      && (!filters.quantization || catalog?.quantizations.includes(filters.quantization))
      && updatedMatches(record, filters.updated, now)
      && (!filters.topology || catalog?.topology_mode === filters.topology || recipe?.topology_name === filters.topology)
      && (!filters.qualification || catalog?.qualification === filters.qualification)
      && (!filters.readiness || catalog?.execution_readiness === filters.readiness)
      && localMatches(record, filters.local)
      && filters.capabilities.every(capability => capabilityValues.includes(capability));
  });
}

export function deriveLibraryModels(records: LibraryRecipeRecord[]): Array<{key: string; title: string; count: number; model?: LibraryModel["model"]}> {
  const models = new Map<string, {key: string; title: string; count: number; model?: LibraryModel["model"]}>();
  for (const record of records) {
    const existing = models.get(record.modelKey);
    if (existing) existing.count += 1;
    else models.set(record.modelKey, {key: record.modelKey, title: record.modelTitle, count: 1, model: record.model});
  }
  return [...models.values()].sort((left, right) => left.title.localeCompare(right.title));
}

function recipeStatus(record: LibraryRecipeRecord): string {
  if (!record.recipe) return "Awaiting automatic sync";
  if (record.recipe.runs.some(run => run.state === "running" && run.healthy)) return "Running";
  if (record.recipe.runs.some(run => run.state === "running")) return "Running · attention";
  if (record.recipe.installations.some(installation => installation.state === "installed")) return "Installed";
  if (record.recipe.selected_revision?.lifecycle === "resolved") return "Available";
  return "Needs review";
}

function localStatus(record: LibraryRecipeRecord): string {
  if (record.withdrawnInstalled) return "Withdrawn upstream · installed";
  if (record.custom) return "Custom recipe";
  const status = record.catalog?.local.status;
  if (status === "update-available") return "Update available";
  if (status === "current") return "Catalog current";
  if (status === "not-imported") return "Pending automatic sync";
  if (status === "different-revision") return "Different revision";
  if (status === "local-ahead") return "Local revision ahead";
  return status === "conflict" ? "Identity conflict" : "Managed catalog";
}

function releaseStatus(record: LibraryRecipeRecord): string {
  if (record.withdrawnInstalled) return "Catalog updates unavailable";
  const catalog = record.catalog;
  if (!catalog) return record.managed ? "Managed catalog recipe" : "Custom recipe";
  if (catalog.local.status === "update-available") return `Update available · v${catalog.local.release_version ?? "?"} → v${catalog.release_version ?? "?"}`;
  if (catalog.local.status === "current") return `v${catalog.release_version ?? catalog.local.release_version ?? "?"} · catalog current`;
  return localStatus(record);
}

function groupForNode(detail: LibraryRecipeDetail, nodeId: string): LibraryPlacementGroup | undefined {
  return detail.placement.flatMap(placement => placement.recommendations).find(group => group.eligible && group.group_complete && group.node_ids.includes(nodeId));
}

function selectionRecipeIds(records: LibraryRecipeRecord[]): Set<string> {
  return new Set(records.flatMap(record => record.recipe ? [record.recipe.recipe_id] : []));
}

export type SparkPlacementState = "available" | "installed" | "running" | "running-attention" | "update" | "withdrawn" | "incompatible" | "offline" | "select";

export function sparkPlacementState(node: VisualFleetNode, selectedRecords: LibraryRecipeRecord[], detail?: LibraryRecipeDetail): SparkPlacementState {
  if (node.connection?.online_state !== "online") return "offline";
  if (selectedRecords.length === 0) return "select";
  const ids = selectionRecipeIds(selectedRecords);
  const withdrawnIds = new Set(selectedRecords.flatMap(record => record.withdrawnInstalled && record.recipe ? [record.recipe.recipe_id] : []));
  if ((node.loaded ?? []).some(run => withdrawnIds.has(run.recipe_id)) || (node.installed ?? []).some(installation => withdrawnIds.has(installation.recipe_id) && installation.rank_state === "installed")) return "withdrawn";
  const selectedRuns = (node.loaded ?? []).filter(run => ids.has(run.recipe_id));
  if (selectedRuns.some(run => run.healthy === false)) return "running-attention";
  if (selectedRuns.length > 0) return "running";
  const installed = (node.installed ?? []).some(installation => ids.has(installation.recipe_id) && installation.rank_state === "installed");
  if (installed && selectedRecords.some(record => record.catalog?.local.status === "update-available")) return "update";
  if (installed) return "installed";
  if (detail && !groupForNode(detail, node.id)) return "incompatible";
  return "available";
}

function stateLabel(state: SparkPlacementState): string {
  if (state === "update") return "Update available";
  if (state === "withdrawn") return "Withdrawn upstream";
  if (state === "running-attention") return "Running · attention";
  if (state === "select") return "Select a model or recipe";
  return state.charAt(0).toUpperCase() + state.slice(1);
}

function modelPath(model: {key: string; model?: LibraryModel["model"]}): string {
  return model.model ? modelLibraryPath(model.key) : model.key === "unlinked" ? unlinkedLibraryPath() : `/library?model_version=${encodeURIComponent(model.key)}`;
}

function valueOptions(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.flatMap(value => value ? [value] : []))].sort();
}

function formattedSyncTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
}

function FilterSelect({label, onChange, options, value}: {label: string; onChange(value: string): void; options: Array<{label: string; value: string}>; value: string}) {
  return <label><span>{label}</span><select aria-label={label} value={value} onChange={event => onChange(event.target.value)}><option value="">All</option>{options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>;
}

export function LibraryWorkcell({
  api, catalogCommit, catalogRepository, detail, detailContent, detailError, detailLoading, fleet, fleetError, filters, onBusyChange,
  onFiltersChange, onNavigate, onRefresh, onRetryDetail, onRetryFleet, publicRecipes, query, route, snapshot, syncAvailable, syncError, syncing, syncSummary, onSyncNow, windowed,
}: {
  api: LibraryApi;
  catalogCommit?: string;
  catalogRepository?: string;
  detail?: LibraryRecipeDetail;
  detailContent?: ReactNode;
  detailError: string;
  detailLoading: boolean;
  fleet?: VisualFleetSnapshot;
  fleetError: string;
  filters: LibraryWorkcellFilters;
  onBusyChange?(busy: boolean): void;
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate: Navigate;
  onRefresh(signal: AbortSignal): Promise<void>;
  onRetryFleet(): void;
  onRetryDetail(): void;
  onSyncNow(): void;
  publicRecipes: PublicRecipe[];
  query: string;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
  syncAvailable: boolean;
  syncError: string;
  syncing: boolean;
  syncSummary?: ManagedCatalogSyncSummary;
  windowed: boolean;
}) {
  const [placementRecipe, setPlacementRecipe] = useState<LibraryRecipeRecord>();
  const [draggedRecipe, setDraggedRecipe] = useState<LibraryRecipeRecord>();
  const [dropNodeId, setDropNodeId] = useState("");
  const [placementDetail, setPlacementDetail] = useState<LibraryRecipeDetail>();
  const [placementError, setPlacementError] = useState("");
  const [placementLoading, setPlacementLoading] = useState(false);
  const [review, setReview] = useState<{invocation: LibraryPlacementInvocation; nodeIds: string[]; record: LibraryRecipeRecord}>();
  const [recipeRemovalId, setRecipeRemovalId] = useState("");
  const [modelDeletionOpen, setModelDeletionOpen] = useState(false);
  const [removalOperation, setRemovalOperation] = useState<LibraryOperation>();
  const [announcement, setAnnouncement] = useState("");
  const placementTrigger = useRef<HTMLButtonElement | null>(null);
  const removalTrigger = useRef<HTMLButtonElement | null>(null);
  const modelDeletionTrigger = useRef<HTMLButtonElement | null>(null);
  const placementRequest = useRef(0);
  const allRecords = useMemo(
    () => applyManagedCatalogWithdrawals(buildLibraryRecipeRecords(snapshot, publicRecipes), fleet, syncSummary?.withdrawn_recipes ?? []),
    [fleet, publicRecipes, snapshot, syncSummary?.withdrawn_recipes],
  );
  const matchingRecords = useMemo(() => filterLibraryRecipeRecords(allRecords, filters, query), [allRecords, filters, query]);
  const models = useMemo(() => deriveLibraryModels(matchingRecords), [matchingRecords]);
  const selectedRecord = route.kind === "recipe" ? allRecords.find(record => record.recipe?.recipe_id === route.recipeId) : undefined;
  const selectedModelKey = route.kind === "model" ? (route.unlinked ? "unlinked" : route.modelKey) : selectedRecord?.modelKey;
  const visibleRecords = route.kind === "model" && route.unlinked
    ? matchingRecords.filter(record => record.modelKey === "unlinked")
    : selectedModelKey ? matchingRecords.filter(record => record.modelKey === selectedModelKey) : matchingRecords;
  const selectedRecords = selectedRecord ? [selectedRecord] : selectedModelKey ? allRecords.filter(record => record.modelKey === selectedModelKey) : [];
  const selectedModelIdentity = selectedRecords.find(record => record.model)?.model;
  const selectedModelRecipeIds = selectionRecipeIds(selectedRecords);
  const selectedModelNodeIds = [...new Set(fleet?.nodes.flatMap(node => (node.installed ?? []).some(installation => selectedModelRecipeIds.has(installation.recipe_id) && installation.rank_state === "installed") || (node.loaded ?? []).some(run => selectedModelRecipeIds.has(run.recipe_id)) ? [node.id] : []) ?? [])];
  const selectedModelInstalledRecipeCount = new Set(fleet?.nodes.flatMap(node => [
    ...(node.installed ?? []).flatMap(installation => selectedModelRecipeIds.has(installation.recipe_id) && installation.rank_state === "installed" ? [installation.recipe_id] : []),
    ...(node.loaded ?? []).flatMap(run => selectedModelRecipeIds.has(run.recipe_id) ? [run.recipe_id] : []),
  ]) ?? []).size;
  const activeDetail = detail && selectedRecord?.recipe && detail.recipe.recipe_id === selectedRecord.recipe.recipe_id ? detail : placementDetail;
  const selectedRecipeDetail = detail && selectedRecord?.recipe && detail.recipe.recipe_id === selectedRecord.recipe.recipe_id ? detail : undefined;
  const selectedInstallations = selectedRecipeDetail?.operational_state.installations.filter(installation => installation.state !== "uninstalled") ?? [];
  const selectedActiveRuns = selectedRecipeDetail?.operational_state.runs.filter(run => ["running", "published"].includes(run.state)) ?? [];
  const selectedHasFleetRun = Boolean(selectedRecord?.recipe && fleet?.nodes.some(node => (node.loaded ?? []).some(run => run.recipe_id === selectedRecord.recipe?.recipe_id)));
  const selectedHasActiveRun = selectedActiveRuns.length > 0 || selectedHasFleetRun;
  const creators = valueOptions(publicRecipes.map(recipe => recipe.source_owner));
  const repositories = valueOptions(publicRecipes.map(recipe => recipe.source_repository));
  const runtimes = valueOptions(publicRecipes.map(recipe => recipe.runtime_distribution));
  const quantizations = valueOptions(publicRecipes.flatMap(recipe => recipe.quantizations));
  const topologies = valueOptions(publicRecipes.map(recipe => recipe.topology_mode));
  const modelFamilies = [...new Map(publicRecipes.map(recipe => [`${recipe.model_publisher}/${recipe.model_slug}`, recipe.model_title])).entries()];
  const modelVersions = [...new Map(allRecords.map(record => [record.modelKey, record.modelTitle])).entries()];
  const appliedFilterCount = Object.entries(filters).reduce((count, [, value]) => count + (Array.isArray(value) ? value.length : value ? 1 : 0), 0);
  const installedWithdrawalCount = new Set(allRecords.flatMap(record => record.withdrawnInstalled && record.recipe ? [record.recipe.recipe_id] : [])).size;
  const syncTime = formattedSyncTime(syncSummary?.completed_at ?? null);
  const staleInstallationCount = syncSummary?.stale_recipes.reduce((count, item) => count + item.stale_installation_count, 0) ?? 0;
  const staleRunCount = syncSummary?.stale_recipes.reduce((count, item) => count + item.stale_run_count, 0) ?? 0;
  const syncState = syncing ? "Updating…"
    : syncError ? "Update needs attention"
      : syncSummary?.state === "partial" ? "Updated with items to review"
        : syncSummary?.state === "failed" ? "Automatic update failed"
          : syncSummary?.state === "syncing" ? "Automatic update in progress"
            : syncSummary?.state === "current" ? "Vonk Forge remote is current"
              : syncAvailable ? "Automatic updates enabled" : "Catalog discovery only";

  useEffect(() => {
    setRecipeRemovalId("");
    setRemovalOperation(undefined);
    setModelDeletionOpen(false);
  }, [selectedRecord?.key]);

  useEffect(() => { setModelDeletionOpen(false); }, [selectedModelKey]);

  function updateFilter<K extends keyof LibraryWorkcellFilters>(key: K, value: LibraryWorkcellFilters[K]) {
    onFiltersChange({...filters, [key]: value});
  }

  async function preparePlacement(record: LibraryRecipeRecord) {
    const requestId = ++placementRequest.current;
    setPlacementRecipe(record);
    setPlacementError("");
    setAnnouncement(`Checking compatible Spark groups for ${record.title}.`);
    if (!record.recipe) return;
    if (detail?.recipe.recipe_id === record.recipe.recipe_id) {
      setPlacementDetail(detail);
      setAnnouncement(`Choose a compatible Spark for ${record.title}.`);
      return;
    }
    if (placementDetail?.recipe.recipe_id === record.recipe.recipe_id) {
      setAnnouncement(`Choose a compatible Spark for ${record.title}.`);
      return;
    }
    setPlacementDetail(undefined);
    setPlacementLoading(true);
    try {
      const nextDetail = await api.libraryRecipe(record.recipe.recipe_id);
      if (placementRequest.current === requestId) {
        setPlacementDetail(nextDetail);
        setAnnouncement(`Choose a compatible Spark for ${record.title}.`);
      }
    } catch (value) {
      if (placementRequest.current === requestId) {
        setPlacementError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load placement authority");
        setAnnouncement(`Compatibility could not be checked for ${record.title}.`);
      }
    } finally {
      if (placementRequest.current === requestId) setPlacementLoading(false);
    }
  }

  async function openPlacement(record: LibraryRecipeRecord, node: VisualFleetNode, trigger: HTMLButtonElement, invocation: LibraryPlacementInvocation) {
    if (!record.recipe || node.connection?.online_state !== "online") return;
    placementTrigger.current = trigger;
    setPlacementLoading(true);
    setPlacementError("");
    try {
      const nextDetail = detail?.recipe.recipe_id === record.recipe.recipe_id
        ? detail
        : placementDetail?.recipe.recipe_id === record.recipe.recipe_id
          ? placementDetail
          : await api.libraryRecipe(record.recipe.recipe_id);
      setPlacementDetail(nextDetail);
      const group = groupForNode(nextDetail, node.id);
      if (!group) {
        setPlacementError(`${node.display_name || node.hostname} is not in a compatible complete placement group for ${record.title}.`);
        return;
      }
      setReview({invocation, nodeIds: [...group.node_ids].sort(), record});
    } catch (value) {
      setPlacementError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load placement authority");
    } finally {
      setPlacementLoading(false);
    }
  }

  function onDragStart(event: DragEvent<HTMLElement>, record: LibraryRecipeRecord) {
    if (!record.recipe) { event.preventDefault(); return; }
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData("text/plain", record.recipe.recipe_id);
    setDraggedRecipe(record);
    void preparePlacement(record);
  }

  function closeReview() {
    setReview(undefined);
    queueMicrotask(() => placementTrigger.current?.focus());
  }

  function closeRecipeRemoval() {
    setRecipeRemovalId("");
    queueMicrotask(() => removalTrigger.current?.focus());
  }

  function closeModelDeletion() {
    setModelDeletionOpen(false);
    queueMicrotask(() => modelDeletionTrigger.current?.focus());
  }

  const railRecord = draggedRecipe ?? placementRecipe ?? selectedRecord;
  return <>
    <section className="library-sync-strip" aria-label="Managed catalog synchronization">
      <div><strong>Managed recipes update automatically</strong><span>{catalogRepository ? `${catalogRepository}${catalogCommit ? ` · ${catalogCommit.slice(0, 8)}` : ""}` : "Waiting for Vonk Forge remote"}</span></div>
      <div className="library-sync-status"><span>{syncState}</span><button type="button" className="button secondary" disabled={!syncAvailable || syncing || syncSummary?.state === "syncing"} title={!syncAvailable ? "The Controller does not expose managed recipe synchronization." : undefined} onClick={onSyncNow}>{syncing ? "Updating from remote…" : "Update from Vonk Forge remote"}</button></div>
      {syncSummary && <p role="status">Last update: {syncSummary.imported_count} imported · {syncSummary.updated_count} updated · {syncSummary.unchanged_count} unchanged{syncTime ? ` · ${syncTime}` : ""}</p>}
      {(staleInstallationCount > 0 || staleRunCount > 0) && <p className="is-warning" role="status">{staleInstallationCount} installed placement{staleInstallationCount === 1 ? " uses" : "s use"} an older recipe revision · {staleRunCount} active run{staleRunCount === 1 ? " needs" : "s need"} review.</p>}
      {syncSummary && syncSummary.problems.length > 0 && <details className="library-sync-problems"><summary>{syncSummary.problems.length} update item{syncSummary.problems.length === 1 ? "" : "s"} need review</summary><ul>{syncSummary.problems.map((problem, index) => <li key={`${problem.recipe_uri}-${problem.code}-${index}`}><strong>{problem.code}</strong><span>{problem.detail}</span></li>)}</ul></details>}
      {installedWithdrawalCount > 0 && <p className="is-warning" role="status">{installedWithdrawalCount} installed recipe{installedWithdrawalCount === 1 ? " is" : "s are"} withdrawn upstream. Installed content remains pinned until you review its removal.</p>}
      {syncError && <p className="is-error" role="alert">{syncError}</p>}
    </section>
    <details className="library-filter-board" open={appliedFilterCount > 0}>
      <summary><span><strong>Recipe filters</strong><small>{appliedFilterCount ? `${appliedFilterCount} applied · models derive from matching recipes` : "Model, creator, topology, runtime, status, and capability"}</small></span><span aria-hidden="true" className="library-filter-disclosure">Expand</span></summary>
      <div className="library-filter-grid">
        <FilterSelect label="Model type" value={filters.modelType} onChange={value => updateFilter("modelType", value as ModelType)} options={[
          {value: "language", label: "Language / chat"}, {value: "vision", label: "Vision / multimodal"}, {value: "image", label: "Image"}, {value: "video", label: "Video"}, {value: "audio", label: "Audio"}, {value: "3d", label: "3D"},
        ]}/>
        <FilterSelect label="Model" value={filters.model} onChange={value => updateFilter("model", value)} options={modelFamilies.map(([value, label]) => ({value, label}))}/>
        <FilterSelect label="Model version" value={filters.modelVersion} onChange={value => updateFilter("modelVersion", value)} options={modelVersions.map(([value, label]) => ({value, label}))}/>
        <FilterSelect label="Recipe creator" value={filters.sourceOwner} onChange={value => updateFilter("sourceOwner", value)} options={creators.map(value => ({value, label: value}))}/>
        <FilterSelect label="Repository" value={filters.repository} onChange={value => updateFilter("repository", value)} options={repositories.map(value => ({value, label: value.replace(/^https?:\/\//, "")}))}/>
        <FilterSelect label="Spark count" value={filters.sparks} onChange={value => updateFilter("sparks", value as SparkFilter)} options={[{value: "1", label: "1 Spark"}, {value: "2", label: "2 Sparks"}, {value: "3", label: "3 Sparks"}, {value: "4+", label: "4+ Sparks"}]}/>
        <FilterSelect label="Runtime" value={filters.runtime} onChange={value => updateFilter("runtime", value)} options={runtimes.map(value => ({value, label: humanizeIdentifier(value)}))}/>
        <FilterSelect label="Quantization" value={filters.quantization} onChange={value => updateFilter("quantization", value)} options={quantizations.map(value => ({value, label: value}))}/>
        <FilterSelect label="Updated" value={filters.updated} onChange={value => updateFilter("updated", value as UpdatedFilter)} options={[{value: "7", label: "Last 7 days"}, {value: "30", label: "Last 30 days"}, {value: "90", label: "Last 90 days"}, {value: "365", label: "Last year"}]}/>
        <FilterSelect label="Topology" value={filters.topology} onChange={value => updateFilter("topology", value)} options={topologies.map(value => ({value, label: humanizeIdentifier(value)}))}/>
        <FilterSelect label="Qualification" value={filters.qualification} onChange={value => updateFilter("qualification", value as LibraryWorkcellFilters["qualification"])} options={[{value: "cataloged", label: "Accepted"}, {value: "candidate", label: "Candidate"}]}/>
        <FilterSelect label="Execution readiness" value={filters.readiness} onChange={value => updateFilter("readiness", value as LibraryWorkcellFilters["readiness"])} options={[{value: "executable", label: "Executable"}, {value: "integration-required", label: "Integration required"}, {value: "not-executable", label: "Not executable"}, {value: "not-declared", label: "Not declared"}]}/>
        <FilterSelect label="Local status" value={filters.local} onChange={value => updateFilter("local", value as LocalFilter)} options={[{value: "current", label: "Current"}, {value: "update-available", label: "Update available"}, {value: "withdrawn", label: "Withdrawn · installed"}, {value: "not-imported", label: "Pending sync"}, {value: "needs-review", label: "Needs review"}, {value: "custom", label: "Custom recipes"}]}/>
      </div>
      <fieldset className="library-capability-filters"><legend>Capabilities</legend>{CAPABILITIES.map(capability => <label key={capability.value}><input type="checkbox" checked={filters.capabilities.includes(capability.value)} onChange={() => updateFilter("capabilities", filters.capabilities.includes(capability.value) ? filters.capabilities.filter(value => value !== capability.value) : [...filters.capabilities, capability.value])}/><span>{capability.label}</span></label>)}</fieldset>
      {appliedFilterCount > 0 && <button type="button" className="button secondary library-clear-filters" onClick={() => onFiltersChange(EMPTY_LIBRARY_WORKCELL_FILTERS)}>Clear recipe filters</button>}
    </details>
    <div className={`library-workcell route-${route.kind}`}>
      <section className="library-pane library-models" aria-label="Models">
        <div className="library-pane-heading"><div><h3>Models</h3></div><small>{models.length} from {matchingRecords.length} recipes</small></div>
        <div className="library-list">
          {models.map(model => {
            const withdrawalCount = matchingRecords.filter(record => record.modelKey === model.key && record.withdrawnInstalled).length;
            return <article className="library-row-shell library-model-row" key={model.key}><a href={modelPath(model)} className="library-row" aria-current={selectedModelKey === model.key ? "page" : undefined} onClick={event => onNavigate(event, modelPath(model))}><strong>{model.title}</strong><span>Model version</span><small>{model.count} recipe{model.count === 1 ? "" : "s"}{windowed ? " shown" : ""}</small>{withdrawalCount > 0 && <span className="library-withdrawn-label">Installed recipe withdrawn upstream</span>}</a>{model.model && <div className="library-row-tools"><TechnicalDetails compact items={[{label: "Publisher", value: model.model.publisher}, {label: "Model slug", value: model.model.slug}, {label: "Model digest", value: model.model.content_sha256}]}/></div>}</article>;
          })}
          {models.length === 0 && <p className="library-placeholder">No models have recipes matching these filters.</p>}
        </div>
      </section>
      <section className="library-pane library-recipes" aria-label={selectedModelKey === "unlinked" ? "Unlinked recipes" : selectedModelKey ? `Recipes for ${models.find(model => model.key === selectedModelKey)?.title ?? selectedRecord?.modelTitle ?? "selected model"}` : "Recipe inventory"}>
        {route.kind === "model" && <a className="library-back" href="/library" onClick={event => onNavigate(event, "/library")}>Back to Models</a>}
        {route.kind === "recipe" && <a className="library-back" href={selectedModelKey === "unlinked" ? unlinkedLibraryPath() : selectedRecord?.model ? modelLibraryPath(selectedRecord.modelKey) : "/library"} onClick={event => onNavigate(event, selectedModelKey === "unlinked" ? unlinkedLibraryPath() : selectedRecord?.model ? modelLibraryPath(selectedRecord.modelKey) : "/library")}>Back to {selectedModelKey === "unlinked" ? "Unlinked" : selectedRecord?.modelTitle ?? "Model"} recipes</a>}
        <div className="library-pane-heading"><div><h3>{selectedModelKey ? selectedRecord?.modelTitle ?? models.find(model => model.key === selectedModelKey)?.title ?? "Recipes" : "Recipe work surface"}</h3></div><small>{visibleRecords.length} shown</small></div>
        <div className="library-recipe-worklist" role="list" aria-label="Filtered recipes">
          {visibleRecords.map(record => {
            const active = selectedRecord?.key === record.key;
            return <article role="listitem" key={record.key} className={`library-workcell-recipe${active ? " is-selected" : ""}${record.custom ? " is-custom" : ""}`} draggable={Boolean(record.recipe)} onDragStart={event => onDragStart(event, record)} onDragEnd={() => { setDraggedRecipe(undefined); setDropNodeId(""); }}>
              <div className="library-workcell-recipe-main">
                {record.recipe ? <a className="library-row" aria-current={active ? "page" : undefined} href={recipeLibraryPath(record.recipe.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(record.recipe!.recipe_id))}><strong>{record.title}</strong><span>{record.recipe.description}</span></a> : <a className="library-row" href={`/library/import?recipe=${encodeURIComponent(record.catalog!.uri)}`} onClick={event => onNavigate(event, `/library/import?recipe=${encodeURIComponent(record.catalog!.uri)}`)}><strong>{record.title}</strong><span>{record.catalog?.description}</span></a>}
                <div className="library-workcell-recipe-signals"><span className={`recipe-origin ${record.custom ? "is-custom" : "is-managed"}`}>{record.custom ? "Custom" : "Managed"}</span>{record.withdrawnInstalled && <span className="is-withdrawn">Withdrawn upstream · installed</span>}{record.catalog && <span className={`library-qualification qualification-${record.catalog.qualification}`}>{record.catalog.qualification === "cataloged" ? "Accepted" : "Candidate"}</span>}<span>{releaseStatus(record)}</span><span>{recipeStatus(record)}</span>{record.catalog && <span>{record.catalog.node_count} Spark{record.catalog.node_count === 1 ? "" : "s"}</span>}</div>
              </div>
              <div className="library-workcell-recipe-actions">{record.catalog?.local.status === "update-available" && <a aria-label={`Review update for ${record.title}`} className="library-release-link" href={`/library/import?recipe=${encodeURIComponent(record.catalog.uri)}`} onClick={event => onNavigate(event, `/library/import?recipe=${encodeURIComponent(record.catalog!.uri)}`)}>Review update</a>}<button type="button" className="button secondary" disabled={!record.recipe} title={!record.recipe ? "Automatic catalog sync must create the local immutable revision first." : undefined} onClick={event => { placementTrigger.current = event.currentTarget; void preparePlacement(record); }}>{placementRecipe?.key === record.key ? (placementLoading ? "Checking Sparks…" : "Choose a Spark") : "Place on Spark"}</button>{record.recipe && <span className="drag-hint">Drag to a compatible Spark</span>}</div>
            </article>;
          })}
          {visibleRecords.length === 0 && <div className="library-workcell-empty"><strong>No recipes match</strong><p>Clear a filter or choose another model. Custom recipes remain available under Local status.</p></div>}
        </div>
      </section>
      <aside className="library-pane library-spark-rail" aria-label="Sparks">
        <div className="library-pane-heading"><div><h3>Sparks</h3></div><small>{fleet?.nodes.length ?? 0} enrolled</small></div>
        {selectedRecord && selectedInstallations.length > 0 && <section className="library-removal-overview" aria-label={`Remove ${selectedRecord.title}`}>
          <div><strong>Installed recipe</strong><span>{selectedRecord.title} occupies {new Set(selectedInstallations.flatMap(installation => installation.node_ids)).size} Spark{new Set(selectedInstallations.flatMap(installation => installation.node_ids)).size === 1 ? "" : "s"}.</span></div>
          {selectedHasActiveRun && <p className="is-warning">Active runs must stop before removal. You can still review the exact blocked plan.</p>}
          {selectedInstallations.map((installation, index) => <div className="library-removal-placement" key={installation.installation_id}>
            <span>Placement {selectedInstallations.length > 1 ? index + 1 : ""} · {installation.node_ids.map(nodeId => fleet?.nodes.find(node => node.id === nodeId)?.display_name ?? nodeId).join(" + ")}</span>
            <button type="button" className="button secondary" disabled={removalOperation !== undefined && !operationSettled(removalOperation.state)} onClick={event => { removalTrigger.current = event.currentTarget; setRecipeRemovalId(installation.installation_id); }}>Review recipe removal</button>
          </div>)}
          <small>The catalog recipe remains available. The Controller preview decides whether shared model files stay on each Spark.</small>
        </section>}
        {route.kind === "model" && selectedModelIdentity && selectedModelNodeIds.length > 0 && <section className="library-removal-overview library-model-removal-overview" aria-label={`Delete ${selectedRecords[0]?.modelTitle ?? "selected model"}`}>
          <div><strong>Installed model</strong><span>{selectedRecords[0]?.modelTitle ?? "This exact model"} is used by {selectedModelInstalledRecipeCount} installed recipe{selectedModelInstalledRecipeCount === 1 ? "" : "s"} across {selectedModelNodeIds.length} Spark{selectedModelNodeIds.length === 1 ? "" : "s"}.</span></div>
          <p>Deleting the model also removes every installed recipe that relies on it. Active runs block deletion and are never stopped automatically.</p>
          <button ref={modelDeletionTrigger} type="button" className="button secondary" onClick={() => setModelDeletionOpen(true)}>Review model deletion</button>
          <small>Shared unrelated caches remain protected. The server preview lists every affected placement before confirmation.</small>
        </section>}
        {removalOperation && <LibraryOperationProgress api={api} name="Remove" onChange={setRemovalOperation} onRefresh={onRefresh} operation={removalOperation}/>}
        {!fleet && !fleetError && <p className="library-placeholder" role="status">Loading live Spark state…</p>}
        {fleetError && <div className="library-spark-error" role="alert"><p>{fleetError}</p><button type="button" className="button secondary" onClick={onRetryFleet}>Retry Sparks</button></div>}
        {placementError && <div className="library-spark-error" role="alert"><p>{placementError}</p><button type="button" className="button secondary" onClick={() => setPlacementError("")}>Dismiss</button></div>}
        <div className="library-spark-list" aria-busy={placementLoading || undefined}>{fleet?.nodes.map(node => {
          const stateRecords = railRecord ? [railRecord] : selectedRecords;
          const stateRecipeIds = selectionRecipeIds(stateRecords);
          const activeSelectedRun = (node.loaded ?? []).some(run => stateRecipeIds.has(run.recipe_id));
          const runningRecords = stateRecords.filter(record => record.recipe && (node.loaded ?? []).some(run => run.recipe_id === record.recipe?.recipe_id));
          const installedRecords = stateRecords.filter(record => record.recipe && !runningRecords.includes(record) && (node.installed ?? []).some(installation => installation.recipe_id === record.recipe?.recipe_id && installation.rank_state === "installed"));
          const projectedState = sparkPlacementState(node, stateRecords, activeDetail);
          const group = activeDetail ? groupForNode(activeDetail, node.id) : undefined;
          const groupReady = Boolean(group?.node_ids.every(nodeId => fleet.nodes.some(item => item.id === nodeId && item.connection?.online_state === "online")));
          const state = railRecord && activeDetail && !["running", "running-attention", "installed", "update", "withdrawn", "offline"].includes(projectedState) && !groupReady ? "incompatible" : projectedState;
          const compatibilityPending = Boolean(railRecord?.recipe) && placementLoading && !activeDetail;
          const compatible = Boolean(railRecord?.recipe) && node.connection?.online_state === "online" && Boolean(activeDetail && groupReady);
          const atomicNodes = group?.node_ids ?? [];
          const dropActive = dropNodeId === node.id;
          return <article key={node.id} className={`library-spark-target state-${state}${dropActive ? " is-drop-active" : ""}${compatibilityPending ? " is-drop-pending" : ""}${(draggedRecipe || placementRecipe) && !compatible && !compatibilityPending ? " is-drop-incompatible" : ""}`} onDragEnter={event => { event.preventDefault(); setDropNodeId(node.id); }} onDragOver={event => { if (compatible) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } }} onDragLeave={() => setDropNodeId(current => current === node.id ? "" : current)} onDrop={event => { event.preventDefault(); setDropNodeId(""); const record = draggedRecipe ?? placementRecipe; setDraggedRecipe(undefined); const trigger = event.currentTarget.querySelector<HTMLButtonElement>(".library-place-target"); if (record && compatible && trigger) void openPlacement(record, node, trigger, "drag-drop"); }}>
            <header><div><strong>{node.display_name || node.hostname}</strong><span>{node.hostname}</span></div><span className={`library-spark-state state-${state}`}>{stateLabel(state)}</span></header>
            <dl><div><dt>Connection</dt><dd>{node.connection?.online_state ?? "Unknown"}</dd></div><div><dt>Workloads</dt><dd>{(node.loaded ?? []).length} running · {(node.installed ?? []).length} installed</dd></div>{node.inventory && <div><dt>Disk free</dt><dd>{formatBytes(node.inventory.disk_free_bytes)}</dd></div>}</dl>
            {(runningRecords.length > 0 || installedRecords.length > 0) && <div className="library-selected-locations" role="group" aria-label={`Selected content on ${node.display_name || node.hostname}`}>{runningRecords.map(record => { const run = (node.loaded ?? []).find(item => item.recipe_id === record.recipe?.recipe_id); return <p key={`run-${record.key}`}><strong>{run?.healthy === false ? "Running · attention" : "Running"}</strong><span>{record.title}</span></p>; })}{installedRecords.map(record => <p key={`installed-${record.key}`}><strong>Installed</strong><span>{record.title}</span></p>)}</div>}
            {atomicNodes.length > 1 && <p className="library-atomic-placement"><strong>Atomic {atomicNodes.length}-Spark placement</strong><span>{atomicNodes.map(nodeId => fleet.nodes.find(item => item.id === nodeId)?.display_name ?? nodeId).join(" + ")}</span></p>}
            {state === "withdrawn" && <p className="library-withdrawn-impact"><strong>Installed content is withdrawn upstream</strong><span>{activeSelectedRun ? "This content is running. Stop the complete run before reviewing removal." : "This exact local content stays pinned and cannot receive further catalog updates."}</span></p>}
            {(draggedRecipe || placementRecipe) && <button type="button" className="button secondary library-place-target" disabled={!compatible || placementLoading} onClick={event => { const record = draggedRecipe ?? placementRecipe; if (record) void openPlacement(record, node, event.currentTarget, event.detail === 0 ? "keyboard" : "button"); }}>{compatibilityPending ? "Checking compatibility…" : compatible ? `Review placement on ${node.display_name || node.hostname}` : `${state === "offline" ? "Offline" : "Incompatible"} for placement`}</button>}
          </article>;
        })}</div>
        {fleet && fleet.nodes.length === 0 && <div className="library-workcell-empty"><strong>No Sparks enrolled</strong><p>Enroll a Spark before previewing placement.</p></div>}
      </aside>
      {route.kind === "recipe" && selectedRecord && <section className="library-pane library-detail library-inline-detail library-workcell-detail" aria-label="Recipe detail">
        <div className="library-inline-detail-heading"><div><h3>{selectedRecord.title}</h3><span>Exact recipe authority</span></div><span>Full operational workspace</span></div>
        {detailLoading && <p role="status">Loading exact recipe authority…</p>}
        {detailError && <div className="fleet-error" role="alert"><p>{detailError}</p><button type="button" onClick={onRetryDetail}>Retry recipe detail</button></div>}
        {detailContent}
      </section>}
    </div>
    <p className="visually-hidden" role="status" aria-live="polite">{announcement}</p>
    {review?.record.recipe && <LibraryPlacementDialog
      api={api}
      invocation={review.invocation}
      nodeIds={review.nodeIds}
      nodeNames={Object.fromEntries(fleet?.nodes.map(node => [node.id, node.display_name || node.hostname]) ?? [])}
      onBusyChange={onBusyChange}
      onClose={closeReview}
      onRefresh={onRefresh}
      recipeId={review.record.recipe.recipe_id}
      recipeTitle={review.record.title}
    />}
    {selectedRecord && recipeRemovalId && <LibraryActionDialog
      alias={selectedRecord.recipe?.slug ?? selectedRecord.title}
      api={api}
      onApplied={operation => setRemovalOperation(operation)}
      onBusyChange={onBusyChange}
      onClose={closeRecipeRemoval}
      onRefresh={onRefresh}
      policy={snapshot.freshness_policy}
      target={{kind: "uninstall", installationId: recipeRemovalId}}
    />}
    {modelDeletionOpen && selectedModelIdentity && <LibraryModelDeletionDialog
      api={api}
      modelTitle={selectedRecords[0]?.modelTitle ?? friendlyModelName(selectedModelIdentity)}
      modelVersionSha256={selectedModelIdentity.content_sha256}
      nodeNames={Object.fromEntries(fleet?.nodes.map(node => [node.id, node.display_name || node.hostname]) ?? [])}
      onBusyChange={onBusyChange}
      onClose={closeModelDeletion}
      onRefresh={onRefresh}
    />}
  </>;
}

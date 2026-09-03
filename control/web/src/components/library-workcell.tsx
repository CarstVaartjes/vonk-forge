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
import {formatBytes, nodeDisplayName} from "../lib/fleet";
import type {LibraryRoute} from "../lib/library-route";
import {modelVersionKey, recipeLibraryPath} from "../lib/library-route";
import type {LibraryPlacementGroup} from "./library-action-types";
import {LibraryActionDialog} from "./library-action-dialog";
import {LibraryModelDeletionDialog} from "./library-model-deletion-dialog";
import {LibraryOperationProgress, operationSettled} from "./library-operation-progress";
import {LibraryPlacementDialog} from "./library-placement-dialog";
import type {LibraryPlacementInvocation} from "./library-placement-dialog";
import {friendlyModelName, humanizeIdentifier} from "./library-technical-details";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type SparkFilter = "" | "1" | "2" | "3" | "4+";
type UpdatedFilter = "" | "7" | "30" | "90" | "365";
type LocalFilter = "" | PublicRecipe["local"]["status"] | "needs-review" | "custom" | "withdrawn";
type BooleanFilter = "" | "true" | "false";

export type ManagedCatalogWithdrawal = ManagedCatalogSyncSummary["withdrawn_recipes"][number];

export type LibraryWorkcellFilters = {
  abliterated: BooleanFilter;
  capabilities: PublicRecipeCapability[];
  installedOn: string;
  local: LocalFilter;
  model: string;
  modelFamily: string;
  qualification: "" | PublicRecipe["qualification"];
  quantization: string;
  readiness: "" | PublicRecipe["execution_readiness"];
  repository: string;
  runtime: string;
  sourceOwner: string;
  sparks: SparkFilter;
  updated: UpdatedFilter;
};

export const EMPTY_LIBRARY_WORKCELL_FILTERS: LibraryWorkcellFilters = {
  abliterated: "", capabilities: [], installedOn: "", local: "", model: "", modelFamily: "", qualification: "", quantization: "", readiness: "",
  repository: "", runtime: "", sourceOwner: "", sparks: "", updated: "",
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
  const linkedCatalogUris = new Set(records.flatMap(record => record.catalog ? [record.catalog.uri] : []));
  for (const catalog of publicRecipes) {
    if (linkedCatalogUris.has(catalog.uri)) continue;
    records.push({
      catalog,
      custom: false,
      key: catalog.uri,
      managed: true,
      modelKey: `${catalog.model_version_publisher}/${catalog.model_version_slug}`,
      modelTitle: catalog.model_version_title,
      title: catalog.title,
      withdrawnInstalled: false,
    });
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

function modelFamilyTitle(value: string): string {
  const title = value.replace(/\s+[0-9a-f]{8}$/i, "").replace(/^NVIDIA\s+/i, "");
  const variant = /\s+(?:NVFP4|BF16|FP16|FP8|FP4|INT8|INT4|EXL3|AQLM|AWQ|GPTQ|GGUF|TorchAO|DFlash\d*|Abliterated)(?:\s|$)/i.exec(title);
  return (variant ? title.slice(0, variant.index) : title).trim();
}

function recordModelFamily(record: LibraryRecipeRecord): string {
  return modelFamilyTitle(record.catalog?.model_title ?? record.modelTitle);
}

function recordIsAbliterated(record: LibraryRecipeRecord): boolean {
  return record.catalog?.alignment === "abliterated";
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
      && (!filters.modelFamily || recordModelFamily(record) === filters.modelFamily)
      && (!filters.model || record.modelKey === filters.model)
      && (!filters.abliterated || recordIsAbliterated(record) === (filters.abliterated === "true"))
      && (!filters.sourceOwner || catalog?.source_owner === filters.sourceOwner)
      && (!filters.repository || catalog?.source_repository === filters.repository)
      && (!filters.sparks || (filters.sparks === "4+" ? sparks >= 4 : sparks === Number(filters.sparks)))
      && (!filters.runtime || catalog?.runtime_distribution === filters.runtime)
      && (!filters.quantization || catalog?.quantizations.includes(filters.quantization))
      && updatedMatches(record, filters.updated, now)
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

function valueOptions(values: Array<string | null | undefined>): string[] {
  return [...new Set(values.flatMap(value => value ? [value] : []))].sort();
}

function formattedSyncTime(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
}

function installedNodeIds(record: LibraryRecipeRecord, fleet: VisualFleetSnapshot | undefined): string[] {
  if (!record.recipe || !fleet) return [];
  return fleet.nodes.flatMap(node =>
    (node.installed ?? []).some(item => item.recipe_id === record.recipe?.recipe_id && item.rank_state === "installed")
      || (node.loaded ?? []).some(item => item.recipe_id === record.recipe?.recipe_id)
      ? [node.id] : [],
  );
}

function repositoryLabel(value: string): string {
  return value.replace(/^https?:\/\/(?:www\.)?github\.com\//, "").replace(/\/$/, "");
}

export function LibraryWorkcell({
  api, catalogCommit, catalogRepository, detail, detailContent, detailError, detailLoading, fleet, fleetError, filters, onBusyChange,
  onFiltersChange, onNavigate, onRefresh, onRetryDetail, onRetryFleet, publicRecipes, query, route, snapshot, syncError, syncSummary, windowed,
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
  publicRecipes: PublicRecipe[];
  query: string;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
  syncError: string;
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
  const matchingRecords = useMemo(() => filterLibraryRecipeRecords(allRecords, filters, query)
    .filter(record => !filters.installedOn
      || (filters.installedOn === "not-installed" ? installedNodeIds(record, fleet).length === 0 : installedNodeIds(record, fleet).includes(filters.installedOn))), [allRecords, filters, fleet, query]);
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
  const modelFamilies = valueOptions(allRecords.map(recordModelFamily));
  const exactModels = [...new Map(allRecords
    .filter(record => !filters.modelFamily || recordModelFamily(record) === filters.modelFamily)
    .map(record => [record.modelKey, record.modelTitle])).entries()];
  const appliedFilterCount = Object.entries(filters).reduce((count, [, value]) => count + (Array.isArray(value) ? value.length : value ? 1 : 0), 0);
  const installedWithdrawalCount = new Set(allRecords.flatMap(record => record.withdrawnInstalled && record.recipe ? [record.recipe.recipe_id] : [])).size;
  const syncTime = formattedSyncTime(syncSummary?.completed_at ?? null);
  const staleInstallationCount = syncSummary?.stale_recipes.reduce((count, item) => count + item.stale_installation_count, 0) ?? 0;
  const staleRunCount = syncSummary?.stale_recipes.reduce((count, item) => count + item.stale_run_count, 0) ?? 0;
  const syncState = syncError ? "Repository sync needs attention"
    : syncSummary?.state === "partial" ? "Repository synced with items to review"
      : syncSummary?.state === "failed" ? "Automatic repository sync failed"
        : syncSummary?.state === "syncing" ? "Repository sync in progress"
          : syncSummary?.state === "current" ? "Recipe repository is current"
            : "Automatic repository sync enabled";

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
      <div><strong>Recipes stay synchronized automatically</strong><span>{catalogRepository ? `${catalogRepository}${catalogCommit ? ` · ${catalogCommit.slice(0, 8)}` : ""}` : "Waiting for the recipe repository"}</span></div>
      <div className="library-sync-status"><span>{syncState}</span></div>
      {syncSummary && <p role="status">Last repository sync: {syncSummary.imported_count} added · {syncSummary.updated_count} updated · {syncSummary.unchanged_count} unchanged{syncTime ? ` · ${syncTime}` : ""}</p>}
      {(staleInstallationCount > 0 || staleRunCount > 0) && <p className="is-warning" role="status">{staleInstallationCount} installed placement{staleInstallationCount === 1 ? " uses" : "s use"} an older recipe revision · {staleRunCount} active run{staleRunCount === 1 ? " needs" : "s need"} review.</p>}
      {syncSummary && syncSummary.problems.length > 0 && <details className="library-sync-problems"><summary>{syncSummary.problems.length} update item{syncSummary.problems.length === 1 ? "" : "s"} need review</summary><ul>{syncSummary.problems.map((problem, index) => <li key={`${problem.recipe_uri}-${problem.code}-${index}`}><strong>{problem.code}</strong><span>{problem.detail}</span></li>)}</ul></details>}
      {installedWithdrawalCount > 0 && <p className="is-warning" role="status">{installedWithdrawalCount} installed recipe{installedWithdrawalCount === 1 ? " is" : "s are"} withdrawn upstream. Installed content remains pinned until you review its removal.</p>}
      {syncError && <p className="is-error" role="alert">{syncError}</p>}
    </section>
    <div className={`library-workcell route-${route.kind}`}>
      <section className="library-pane library-catalog" aria-label="Recipe catalog">
        <div className="library-pane-heading"><div><h3>Recipes</h3><span>Repository catalog with live Controller placement state</span></div><small>{visibleRecords.length} of {allRecords.length}{windowed ? " shown" : ""}</small></div>
        {appliedFilterCount > 0 && <button type="button" className="button secondary library-clear-filters" onClick={() => onFiltersChange(EMPTY_LIBRARY_WORKCELL_FILTERS)}>Clear filters</button>}
        <div className="library-catalog-table-wrap" tabIndex={0}>
          <table className="library-catalog-table">
            <caption>Recipes synchronized from the repository with live installation locations.</caption>
            <thead><tr>
              <th scope="col"><span>Installed on</span><select aria-label="Installed on" value={filters.installedOn} onChange={event => updateFilter("installedOn", event.target.value)}><option value="">All locations</option><option value="not-installed">Not installed</option>{fleet?.nodes.map(node => <option key={node.id} value={node.id}>{nodeDisplayName(node)}</option>)}</select></th>
              <th scope="col"><span>Recipe</span></th>
              <th scope="col"><span>Model family</span><select aria-label="Model family" value={filters.modelFamily} onChange={event => onFiltersChange({...filters, modelFamily: event.target.value, model: ""})}><option value="">All families</option>{modelFamilies.map(value => <option key={value} value={value}>{value}</option>)}</select></th>
              <th scope="col"><span>Model</span><select aria-label="Model" value={filters.model} onChange={event => updateFilter("model", event.target.value)}><option value="">All models</option>{exactModels.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></th>
              <th scope="col"><span>Format</span><select aria-label="Format" value={filters.quantization} onChange={event => updateFilter("quantization", event.target.value)}><option value="">Any format</option>{quantizations.map(value => <option key={value} value={value}>{value}</option>)}</select></th>
              <th scope="col"><span>Runtime</span><select aria-label="Runtime" value={filters.runtime} onChange={event => updateFilter("runtime", event.target.value)}><option value="">All runtimes</option>{runtimes.map(value => <option key={value} value={value}>{humanizeIdentifier(value)}</option>)}</select></th>
              <th scope="col"><span>Abliterated</span><select aria-label="Abliterated" value={filters.abliterated} onChange={event => updateFilter("abliterated", event.target.value as BooleanFilter)}><option value="">True or False</option><option value="true">True</option><option value="false">False</option></select></th>
              <th scope="col"><span>Sparks</span><select aria-label="Sparks" value={filters.sparks} onChange={event => updateFilter("sparks", event.target.value as SparkFilter)}><option value="">Any count</option><option value="1">1 Spark</option><option value="2">2 Sparks</option><option value="3">3 Sparks</option><option value="4+">4+ Sparks</option></select></th>
              <th scope="col"><span>Creator</span><select aria-label="Creator" value={filters.sourceOwner} onChange={event => updateFilter("sourceOwner", event.target.value)}><option value="">All creators</option>{creators.map(value => <option key={value} value={value}>{value}</option>)}</select></th>
              <th scope="col"><span>Updated</span><select aria-label="Updated" value={filters.updated} onChange={event => updateFilter("updated", event.target.value as UpdatedFilter)}><option value="">Any time</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="365">Last year</option></select></th>
              <th scope="col"><span>Readiness</span><select aria-label="Readiness" value={filters.readiness} onChange={event => updateFilter("readiness", event.target.value as LibraryWorkcellFilters["readiness"])}><option value="">Any readiness</option><option value="executable">Executable</option><option value="integration-required">Integration required</option><option value="not-executable">Not executable</option><option value="not-declared">Not declared</option></select></th>
              <th scope="col"><span>Capabilities</span><select aria-label="Capabilities" value="" onChange={event => { const value = event.target.value as PublicRecipeCapability; if (value && !filters.capabilities.includes(value)) updateFilter("capabilities", [...filters.capabilities, value]); }}><option value="">{filters.capabilities.length ? `${filters.capabilities.length} selected · add…` : "Any capability"}</option>{CAPABILITIES.map(option => <option key={option.value} value={option.value} disabled={filters.capabilities.includes(option.value)}>{option.label}</option>)}</select></th>
              <th scope="col"><span>Qualification</span><select aria-label="Qualification" value={filters.qualification} onChange={event => updateFilter("qualification", event.target.value as LibraryWorkcellFilters["qualification"])}><option value="">Any status</option><option value="cataloged">Accepted</option><option value="candidate">Candidate</option></select></th>
              <th scope="col"><span>Repository</span><select aria-label="Repository" value={filters.repository} onChange={event => updateFilter("repository", event.target.value)}><option value="">All repositories</option>{repositories.map(value => <option key={value} value={value}>{repositoryLabel(value)}</option>)}</select></th>
              <th scope="col"><span>Download</span></th><th scope="col"><span>Disk / Spark</span></th><th scope="col"><span>Memory / Spark</span></th><th scope="col"><span>Action</span></th>
            </tr></thead>
            <tbody>{visibleRecords.map(record => {
              const catalog = record.catalog;
              const locations = installedNodeIds(record, fleet).map(nodeId => { const node = fleet?.nodes.find(item => item.id === nodeId); return node ? nodeDisplayName(node) : nodeId; });
              return <tr className={selectedRecord?.key === record.key ? "is-selected" : ""} key={record.key} draggable={Boolean(record.recipe)} onDragStart={event => onDragStart(event, record)} onDragEnd={() => { setDraggedRecipe(undefined); setDropNodeId(""); }}>
                <td>{locations.length ? locations.join(" · ") : "Not installed"}</td>
                <td>{record.recipe ? <a aria-current={selectedRecord?.key === record.key ? "page" : undefined} href={recipeLibraryPath(record.recipe.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(record.recipe!.recipe_id))}>{record.title}</a> : <strong>{record.title}</strong>}<small>{releaseStatus(record)}</small></td>
                <td>{recordModelFamily(record)}</td><td>{record.modelTitle}</td><td>{catalog?.quantizations.join(" · ") || "—"}</td><td>{catalog ? humanizeIdentifier(catalog.runtime_distribution) : "—"}</td><td>{recordIsAbliterated(record) ? "True" : "False"}</td><td>{catalog?.node_count ?? "—"}</td><td>{catalog?.source_owner ?? "—"}</td><td>{catalog?.release_released_at ?? "—"}</td>
                <td>{catalog ? humanizeIdentifier(catalog.execution_readiness) : "—"}</td><td>{recordCapabilities(record).map(value => CAPABILITIES.find(option => option.value === value)?.label ?? value).join(" · ") || "—"}</td><td>{catalog ? (catalog.qualification === "cataloged" ? "Accepted" : "Candidate") : "Custom"}</td><td>{catalog?.source_repository ? <a href={catalog.source_repository} target="_blank" rel="noreferrer">{repositoryLabel(catalog.source_repository)}<span className="visually-hidden"> opens in a new tab</span></a> : "—"}</td>
                <td>{catalog ? formatBytes(catalog.expected_download_bytes) : "—"}</td><td>{catalog ? formatBytes(catalog.maximum_installed_bytes_per_node) : "—"}</td><td>{catalog ? formatBytes(catalog.maximum_runtime_memory_bytes_per_node) : "—"}</td>
                <td><button type="button" className="button secondary" disabled={!record.recipe} title={!record.recipe ? "Waiting for automatic repository synchronization." : undefined} onClick={event => { placementTrigger.current = event.currentTarget; void preparePlacement(record); }}>{placementRecipe?.key === record.key ? (placementLoading ? "Checking Sparks…" : "Choose a Spark") : record.recipe ? "Place on Spark" : "Syncing…"}</button></td>
              </tr>;
            })}</tbody>
          </table>
          {visibleRecords.length === 0 && <div className="library-workcell-empty"><strong>No recipes match</strong><p>Clear one or more filters to broaden the repository list.</p></div>}
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

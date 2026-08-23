import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {CatalogApi, LibraryApi, LibraryModel, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot, PublicRecipe, PublicRecipePreview} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import {libraryRoute, modelVersionKey} from "../lib/library-route";
import type {LibraryRoute} from "../lib/library-route";
import {parseVisualRecipeDocument} from "../lib/library-recipe-document";
import "./library.css";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;

const defaultDocument = (): VisualRecipeDocument => ({schema_version: 1, identity: {publisher: "local", slug: "custom"}, metadata: {title: "Custom recipe", description: "Custom service recipe", tags: []}, model: {kind: "model-version", publisher: "local", slug: "model", content_sha256: "0".repeat(64)}, execution: {harness: {kind: "execution-harness", publisher: "local", slug: "harness", content_sha256: "1".repeat(64)}, patch_bundle: null}, build: {context: {sha256: "2".repeat(64), expected_bytes: 0, media_type: "application/octet-stream"}, dockerfile: "Dockerfile", platform: "linux/arm64", network_mode: "none", network_hosts: [], download_bytes: 0, temporary_bytes: 0, memory_bytes: 1, timeout_seconds: 1}, artifacts: [], runtime: {distribution: {kind: "runtime-distribution", publisher: "local", slug: "runtime", content_sha256: "3".repeat(64)}, entrypoint: ["run"], lifecycle_pre_start_count: 0, lifecycle_post_stop_count: 0, stop_timeout_seconds: 1}, interfaces: [], validation: {checks: [], benchmark_count: 0}, provenance: {source_kind: "local", source_reference: null, attribution: []} });

function splitList(value: string): string[] {
  return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
}

function joinList(value: readonly string[]): string {
  return value.join(", ");
}

function CustomRecipeForm({document, slug, onSlugChange, onChange}: {
  document: VisualRecipeDocument;
  slug: string;
  onSlugChange(value: string): void;
  onChange(updater: (document: VisualRecipeDocument) => VisualRecipeDocument): void;
}) {
  const update = (updater: (document: VisualRecipeDocument) => VisualRecipeDocument) => onChange(updater);
  const updateArtifact = (index: number, changes: Partial<VisualRecipeDocument["artifacts"][number]>) => update(current => ({...current, artifacts: current.artifacts.map((item, itemIndex) => itemIndex === index ? {...item, ...changes} : item)}));
  const updateInterface = (index: number, changes: Partial<VisualRecipeDocument["interfaces"][number]>) => update(current => ({...current, interfaces: current.interfaces.map((item, itemIndex) => itemIndex === index ? {...item, ...changes} : item)}));
  return <div className="custom-recipe-form">
    <p className="library-form-intro">Fill in the recipe as a readable form. The advanced JSON editor below is available for uncommon fields and import/export.</p>
    <fieldset className="custom-recipe-section">
      <legend>Identity and description</legend>
      <p className="custom-recipe-section-help">Give the recipe a stable name, explain what it runs, and add searchable tags.</p>
      <div className="custom-recipe-fields custom-recipe-fields-two">
        <label>Publisher<input value={document.identity.publisher} onChange={event => update(current => ({...current, identity: {...current.identity, publisher: event.target.value}}))} /></label>
        <label>Recipe slug<input aria-label="Recipe slug" value={slug} onChange={event => { onSlugChange(event.target.value); update(current => ({...current, identity: {...current.identity, slug: event.target.value}})); }} /></label>
        <label>Title<input value={document.metadata.title} onChange={event => update(current => ({...current, metadata: {...current.metadata, title: event.target.value}}))} /></label>
        <label>Tags<input value={joinList(document.metadata.tags)} onChange={event => update(current => ({...current, metadata: {...current.metadata, tags: splitList(event.target.value)}}))} placeholder="chat, gpu, production" /></label>
        <label className="custom-recipe-wide">Description<textarea rows={3} value={document.metadata.description} onChange={event => update(current => ({...current, metadata: {...current.metadata, description: event.target.value}}))} /></label>
      </div>
    </fieldset>

    <fieldset className="custom-recipe-section">
      <legend>Model and execution</legend>
      <p className="custom-recipe-section-help">Choose the exact immutable components that make up this runtime chain.</p>
      <div className="custom-recipe-card-grid">
        <div className="custom-recipe-card"><h4>Model version</h4><div className="custom-recipe-fields"><label>Publisher<input value={document.model.publisher} onChange={event => update(current => ({...current, model: {...current.model, publisher: event.target.value}}))} /></label><label>Slug<input value={document.model.slug} onChange={event => update(current => ({...current, model: {...current.model, slug: event.target.value}}))} /></label><label className="custom-recipe-wide">Content SHA-256<input className="custom-recipe-monospace" value={document.model.content_sha256} onChange={event => update(current => ({...current, model: {...current.model, content_sha256: event.target.value}}))} /></label></div></div>
        <div className="custom-recipe-card"><h4>Execution harness</h4><div className="custom-recipe-fields"><label>Publisher<input value={document.execution.harness.publisher} onChange={event => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, publisher: event.target.value}}}))} /></label><label>Slug<input value={document.execution.harness.slug} onChange={event => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, slug: event.target.value}}}))} /></label><label className="custom-recipe-wide">Content SHA-256<input className="custom-recipe-monospace" value={document.execution.harness.content_sha256} onChange={event => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, content_sha256: event.target.value}}}))} /></label></div></div>
      </div>
      <label className="custom-recipe-checkbox"><input type="checkbox" checked={document.execution.patch_bundle !== null} onChange={event => update(current => ({...current, execution: {...current.execution, patch_bundle: event.target.checked ? {kind: "patch-bundle", publisher: "local", slug: "patch", content_sha256: "4".repeat(64)} : null}}))} /> Use an immutable patch bundle</label>
      {document.execution.patch_bundle && <div className="custom-recipe-card custom-recipe-patch"><h4>Patch bundle</h4><div className="custom-recipe-fields custom-recipe-fields-three"><label>Publisher<input value={document.execution.patch_bundle.publisher} onChange={event => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, publisher: event.target.value} : null}}))} /></label><label>Slug<input value={document.execution.patch_bundle.slug} onChange={event => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, slug: event.target.value} : null}}))} /></label><label>Content SHA-256<input className="custom-recipe-monospace" value={document.execution.patch_bundle.content_sha256} onChange={event => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, content_sha256: event.target.value} : null}}))} /></label></div></div>}
    </fieldset>

    <fieldset className="custom-recipe-section">
      <legend>Build and runtime</legend>
      <p className="custom-recipe-section-help">Set the build boundary, resource expectations, network policy, and process contract.</p>
      <div className="custom-recipe-fields custom-recipe-fields-three">
        <label>Dockerfile<input value={document.build.dockerfile} onChange={event => update(current => ({...current, build: {...current.build, dockerfile: event.target.value}}))} /></label>
        <label>Platform<input value={document.build.platform} onChange={event => update(current => ({...current, build: {...current.build, platform: event.target.value}}))} /></label>
        <label>Network mode<input value={document.build.network_mode} onChange={event => update(current => ({...current, build: {...current.build, network_mode: event.target.value}}))} /></label>
        <label>Context SHA-256<input className="custom-recipe-monospace" value={document.build.context.sha256} onChange={event => update(current => ({...current, build: {...current.build, context: {...current.build.context, sha256: event.target.value}}}))} /></label>
        <label>Context bytes<input type="number" min="0" value={document.build.context.expected_bytes} onChange={event => update(current => ({...current, build: {...current.build, context: {...current.build.context, expected_bytes: Number(event.target.value)}}}))} /></label>
        <label>Context media type<input value={document.build.context.media_type} onChange={event => update(current => ({...current, build: {...current.build, context: {...current.build.context, media_type: event.target.value}}}))} /></label>
        <label>Allowed network hosts<input value={joinList(document.build.network_hosts)} onChange={event => update(current => ({...current, build: {...current.build, network_hosts: splitList(event.target.value)}}))} placeholder="registry.example.com" /></label>
        <label>Download bytes<input type="number" min="0" value={document.build.download_bytes} onChange={event => update(current => ({...current, build: {...current.build, download_bytes: Number(event.target.value)}}))} /></label>
        <label>Temporary bytes<input type="number" min="0" value={document.build.temporary_bytes} onChange={event => update(current => ({...current, build: {...current.build, temporary_bytes: Number(event.target.value)}}))} /></label>
        <label>Memory bytes<input type="number" min="0" value={document.build.memory_bytes} onChange={event => update(current => ({...current, build: {...current.build, memory_bytes: Number(event.target.value)}}))} /></label>
        <label>Timeout seconds<input type="number" min="1" value={document.build.timeout_seconds} onChange={event => update(current => ({...current, build: {...current.build, timeout_seconds: Number(event.target.value)}}))} /></label>
      </div>
      <div className="custom-recipe-subsection"><h4>Runtime distribution</h4><div className="custom-recipe-fields custom-recipe-fields-three"><label>Publisher<input value={document.runtime.distribution.publisher} onChange={event => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, publisher: event.target.value}}}))} /></label><label>Slug<input value={document.runtime.distribution.slug} onChange={event => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, slug: event.target.value}}}))} /></label><label>Content SHA-256<input className="custom-recipe-monospace" value={document.runtime.distribution.content_sha256} onChange={event => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, content_sha256: event.target.value}}}))} /></label><label className="custom-recipe-wide">Entrypoint<input value={joinList(document.runtime.entrypoint)} onChange={event => update(current => ({...current, runtime: {...current.runtime, entrypoint: splitList(event.target.value)}}))} placeholder="run, --config, /etc/service.yaml" /></label><label>Pre-start phases<input type="number" min="0" value={document.runtime.lifecycle_pre_start_count} onChange={event => update(current => ({...current, runtime: {...current.runtime, lifecycle_pre_start_count: Number(event.target.value)}}))} /></label><label>Post-stop phases<input type="number" min="0" value={document.runtime.lifecycle_post_stop_count} onChange={event => update(current => ({...current, runtime: {...current.runtime, lifecycle_post_stop_count: Number(event.target.value)}}))} /></label><label>Stop timeout seconds<input type="number" min="1" value={document.runtime.stop_timeout_seconds} onChange={event => update(current => ({...current, runtime: {...current.runtime, stop_timeout_seconds: Number(event.target.value)}}))} /></label></div></div>
    </fieldset>

    <fieldset className="custom-recipe-section">
      <legend>Artifacts and interfaces</legend>
      <p className="custom-recipe-section-help">Declare downloaded artifacts and the interfaces that operators can reach after startup.</p>
      <div className="custom-recipe-repeat-list">
        {document.artifacts.map((artifact, index) => <div className="custom-recipe-card" key={`${artifact.id}-${index}`}><div className="custom-recipe-card-heading"><h4>Artifact {index + 1}</h4><button type="button" className="button secondary" onClick={() => update(current => ({...current, artifacts: current.artifacts.filter((_, itemIndex) => itemIndex !== index)}))}>Remove</button></div><div className="custom-recipe-fields custom-recipe-fields-three"><label>ID<input value={artifact.id} onChange={event => updateArtifact(index, {id: event.target.value})} /></label><label>Kind<input value={artifact.kind} onChange={event => updateArtifact(index, {kind: event.target.value})} /></label><label>Repository<input value={artifact.repository} onChange={event => updateArtifact(index, {repository: event.target.value})} /></label><label>Revision<input value={artifact.revision} onChange={event => updateArtifact(index, {revision: event.target.value})} /></label><label>Download bytes<input type="number" min="0" value={artifact.download_bytes} onChange={event => updateArtifact(index, {download_bytes: Number(event.target.value)})} /></label><label>Installed bytes<input type="number" min="0" value={artifact.installed_bytes} onChange={event => updateArtifact(index, {installed_bytes: Number(event.target.value)})} /></label><label className="custom-recipe-wide">Roles<input value={joinList(artifact.roles)} onChange={event => updateArtifact(index, {roles: splitList(event.target.value)})} /></label></div></div>)}
        <button type="button" className="button secondary custom-recipe-add" onClick={() => update(current => ({...current, artifacts: [...current.artifacts, {id: `artifact-${current.artifacts.length + 1}`, kind: "model", repository: "", revision: "", download_bytes: 0, installed_bytes: 0, roles: []}]}))}>Add artifact</button>
      </div>
      <div className="custom-recipe-repeat-list">
        {document.interfaces.map((item, index) => <div className="custom-recipe-card" key={`${item.adapter}-${index}`}><div className="custom-recipe-card-heading"><h4>Interface {index + 1}</h4><button type="button" className="button secondary" onClick={() => update(current => ({...current, interfaces: current.interfaces.filter((_, itemIndex) => itemIndex !== index)}))}>Remove</button></div><div className="custom-recipe-fields custom-recipe-fields-three"><label>Adapter<input value={item.adapter} onChange={event => updateInterface(index, {adapter: event.target.value})} /></label><label>Port<input type="number" min="1" value={item.port ?? ""} onChange={event => updateInterface(index, {port: event.target.value ? Number(event.target.value) : null})} /></label><label>Health path<input value={item.health_path ?? ""} onChange={event => updateInterface(index, {health_path: event.target.value || null})} /></label><label>Job path<input value={item.path ?? ""} onChange={event => updateInterface(index, {path: event.target.value || null})} /></label><label>Model aliases<input value={joinList(item.model_aliases ?? [])} onChange={event => updateInterface(index, {model_aliases: splitList(event.target.value)})} /></label></div></div>)}
        <button type="button" className="button secondary custom-recipe-add" onClick={() => update(current => ({...current, interfaces: [...current.interfaces, {adapter: "http", port: 8000, model_aliases: [], health_path: "/health", path: "/v1"}]}))}>Add interface</button>
      </div>
    </fieldset>

    <fieldset className="custom-recipe-section">
      <legend>Validation and provenance</legend>
      <p className="custom-recipe-section-help">Keep the recipe reviewable by recording checks, benchmarks, and where it came from.</p>
      <div className="custom-recipe-fields custom-recipe-fields-two"><label>Validation checks<input value={joinList(document.validation.checks)} onChange={event => update(current => ({...current, validation: {...current.validation, checks: splitList(event.target.value)}}))} placeholder="smoke-test, health-check" /></label><label>Benchmark count<input type="number" min="0" value={document.validation.benchmark_count} onChange={event => update(current => ({...current, validation: {...current.validation, benchmark_count: Number(event.target.value)}}))} /></label><label>Source kind<select value={document.provenance.source_kind} onChange={event => update(current => ({...current, provenance: {...current.provenance, source_kind: event.target.value as VisualRecipeDocument["provenance"]["source_kind"]}}))}><option value="local">Local</option><option value="workload_run">Workload run</option><option value="global">Global</option><option value="fork">Fork</option></select></label><label>Source reference<input value={document.provenance.source_reference ?? ""} onChange={event => update(current => ({...current, provenance: {...current.provenance, source_reference: event.target.value || null}}))} /></label><label className="custom-recipe-wide">Attribution<input value={joinList(document.provenance.attribution)} onChange={event => update(current => ({...current, provenance: {...current.provenance, attribution: splitList(event.target.value)}}))} /></label></div>
    </fieldset>
  </div>;
}

const LIBRARY_MODEL_WINDOW = 40;
const LIBRARY_RECIPE_WINDOW = 50;

type RouteParent =
  | {kind: "model"; model: Omit<LibraryModel, "recipes">; recipe: LibraryRecipeSummary}
  | {kind: "unlinked"; recipe: LibraryRecipeSummary};

function boundedItems<T>(items: T[], limit: number, key: (item: T) => string, pinnedKey?: string): {items: T[]; truncated: boolean} {
  if (items.length <= limit) return {items, truncated: false};
  const window = items.slice(-limit);
  const pinned = pinnedKey ? items.find(item => key(item) === pinnedKey) : undefined;
  if (pinned && !window.some(item => key(item) === pinnedKey)) window.splice(0, 1, pinned);
  return {items: window, truncated: true};
}

function mergeSnapshot(current: LibrarySnapshot | undefined, next: LibrarySnapshot, route: LibraryRoute): {snapshot: LibrarySnapshot; truncated: boolean} {
  const models = new Map((current?.models ?? []).map(model => [modelVersionKey(model.model), {...model, recipes: [...model.recipes]}]));
  for (const model of next.models) {
    const modelKey = modelVersionKey(model.model);
    const existing = models.get(modelKey);
    if (!existing) {
      models.set(modelKey, {...model, recipes: [...model.recipes]});
      continue;
    }
    const recipeIds = new Set(existing.recipes.map(recipe => recipe.recipe_id));
    models.set(modelKey, {
      ...existing,
      recipes: existing.recipes.concat(model.recipes.filter(recipe => !recipeIds.has(recipe.recipe_id))),
    });
  }
  let truncated = false;
  const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
  const modelItems = [...models.values()].map(model => {
    const bounded = boundedItems(model.recipes, LIBRARY_RECIPE_WINDOW, recipe => recipe.recipe_id, recipeId);
    truncated ||= bounded.truncated;
    return {...model, recipes: bounded.items};
  });
  const recipeModel = modelItems.find(
    model => recipeId && model.recipes.some(recipe => recipe.recipe_id === recipeId),
  );
  const selectedModelKey = route.kind === "model" && !route.unlinked
    ? route.modelKey
    : recipeModel ? modelVersionKey(recipeModel.model) : undefined;
  const boundedModels = boundedItems(modelItems, LIBRARY_MODEL_WINDOW, model => modelVersionKey(model.model), selectedModelKey);
  truncated ||= boundedModels.truncated;

  const currentUnlinked = current?.unlinked_recipes ?? [];
  const unlinkedIds = new Set(currentUnlinked.map(recipe => recipe.recipe_id));
  const mergedUnlinked = currentUnlinked.concat(next.unlinked_recipes.filter(recipe => !unlinkedIds.has(recipe.recipe_id)));
  const boundedUnlinked = boundedItems(mergedUnlinked, LIBRARY_RECIPE_WINDOW, recipe => recipe.recipe_id, recipeId);
  truncated ||= boundedUnlinked.truncated;
  return {snapshot: {
    ...(current ?? next),
    models: boundedModels.items,
    next_cursor: next.next_cursor,
    unlinked_recipes: boundedUnlinked.items,
  }, truncated};
}

function routeParent(snapshot: LibrarySnapshot, recipeId: string): RouteParent | undefined {
  for (const model of snapshot.models) {
    const recipe = model.recipes.find(item => item.recipe_id === recipeId);
    if (recipe) {
      const {recipes: _recipes, ...parent} = model;
      return {kind: "model", model: parent, recipe};
    }
  }
  const recipe = snapshot.unlinked_recipes.find(item => item.recipe_id === recipeId);
  return recipe ? {kind: "unlinked", recipe} : undefined;
}

function restoreRouteParent(snapshot: LibrarySnapshot, recipeId: string, parent: RouteParent | undefined): LibrarySnapshot {
  if (!parent || routeParent(snapshot, recipeId)) return snapshot;
  if (parent.kind === "unlinked") {
    const unlinked = boundedItems(
      snapshot.unlinked_recipes.concat(parent.recipe),
      LIBRARY_RECIPE_WINDOW,
      recipe => recipe.recipe_id,
      recipeId,
    );
    return {...snapshot, unlinked_recipes: unlinked.items};
  }

  const existing = snapshot.models.find(model => modelVersionKey(model.model) === modelVersionKey(parent.model.model));
  const restoredModel = existing
    ? {
        ...existing,
        recipes: boundedItems(
          existing.recipes.concat(parent.recipe),
          LIBRARY_RECIPE_WINDOW,
          recipe => recipe.recipe_id,
          recipeId,
        ).items,
      }
    : {...parent.model, recipes: [parent.recipe]};
  const models = existing
    ? snapshot.models.map(model => modelVersionKey(model.model) === modelVersionKey(restoredModel.model) ? restoredModel : model)
    : snapshot.models.concat(restoredModel);
  return {
    ...snapshot,
    models: boundedItems(models, LIBRARY_MODEL_WINDOW, model => modelVersionKey(model.model), modelVersionKey(restoredModel.model)).items,
  };
}

export function LibraryPage({api, path, onNavigate}: {
  api: LibraryApi;
  path: string;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
}) {
  const [snapshot, setSnapshot] = useState<LibrarySnapshot>();
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<LibraryRecipeDetail>();
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const [paginationWindowed, setPaginationWindowed] = useState(false);
  const catalog = api as LibraryApi & Partial<CatalogApi>;
  const [authoring, setAuthoring] = useState<"create" | "import">();
  const [slug, setSlug] = useState(() => defaultDocument().identity.slug);
  const [customDocument, setCustomDocument] = useState<VisualRecipeDocument>(() => defaultDocument());
  const [documentText, setDocumentText] = useState(() => JSON.stringify(defaultDocument(), null, 2));
  const [authoringStatus, setAuthoringStatus] = useState("");
  const [importUri, setImportUri] = useState("");
  const [importPreview, setImportPreview] = useState<PublicRecipePreview>();
  const [importError, setImportError] = useState("");
  const [publicRecipes, setPublicRecipes] = useState<PublicRecipe[]>([]);
  const [publicRecipesLoading, setPublicRecipesLoading] = useState(false);
  const [publicRecipesError, setPublicRecipesError] = useState("");
  const [publicRecipesCommit, setPublicRecipesCommit] = useState("");
  const [publicRecipeQuery, setPublicRecipeQuery] = useState("");
  const [importPreviewLoading, setImportPreviewLoading] = useState(false);
  const [importSaving, setImportSaving] = useState(false);
  const [snapshotAttempt, setSnapshotAttempt] = useState(0);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const loadMoreController = useRef<AbortController | undefined>(undefined);
  const routeParents = useRef(new Map<string, RouteParent>());
  const heading = useRef<HTMLHeadingElement>(null);
  const route = libraryRoute(path);
  const filteredPublicRecipes = useMemo(() => {
    const normalized = publicRecipeQuery.trim().toLowerCase();
    if (!normalized) return publicRecipes;
    return publicRecipes.filter(recipe => [recipe.title, recipe.slug, recipe.description, ...recipe.tags].some(value => value.toLowerCase().includes(normalized)));
  }, [publicRecipeQuery, publicRecipes]);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setPaginationWindowed(false);
    void api.librarySnapshot(undefined, controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        const bounded = mergeSnapshot(undefined, value, route);
        setSnapshot(bounded.snapshot);
        setPaginationWindowed(bounded.truncated);
      })
      .catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Library");
      });
    return () => controller.abort();
  }, [api, snapshotAttempt]);

  useEffect(() => () => loadMoreController.current?.abort(), []);

  async function loadMore() {
    const cursor = snapshot?.next_cursor;
    if (!cursor || loadingMore) return;
    const controller = new AbortController();
    loadMoreController.current?.abort();
    loadMoreController.current = controller;
    setLoadingMore(true);
    setPaginationError("");
    try {
      const next = await api.librarySnapshot(cursor, controller.signal);
      if (!controller.signal.aborted) {
        const bounded = mergeSnapshot(snapshot, next, route);
        setSnapshot(bounded.snapshot);
        if (bounded.truncated) setPaginationWindowed(true);
      }
    } catch (value) {
      if (!controller.signal.aborted) setPaginationError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load more Library recipes");
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
      if (loadMoreController.current === controller) loadMoreController.current = undefined;
    }
  }

  const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
  const refreshDetail = useCallback(async (signal: AbortSignal) => {
    if (!recipeId || signal.aborted) return;
    try {
      const value = await api.libraryRecipe(recipeId, signal);
      if (signal.aborted) return;
      setDetail(value);
      setDetailError("");
    } catch (value) {
      if (!signal.aborted) setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to refresh recipe authority");
    }
  }, [api, recipeId]);
  useEffect(() => {
    setDetail(undefined);
    setDetailError("");
    if (!recipeId) { setDetailLoading(false); return; }
    const controller = new AbortController();
    setDetailLoading(true);
    void api.libraryRecipe(recipeId, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setDetail(value); setDetailLoading(false); } })
      .catch(value => {
        if (!controller.signal.aborted) {
          setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load recipe");
          setDetailLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, detailAttempt, recipeId]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) heading.current?.focus(); });
    return () => { active = false; };
  }, [path]);

  useEffect(() => {
    if (!snapshot || route.kind !== "recipe") return;
    const parent = routeParent(snapshot, route.recipeId);
    if (!parent) return;
    routeParents.current.delete(route.recipeId);
    routeParents.current.set(route.recipeId, parent);
    while (routeParents.current.size > LIBRARY_RECIPE_WINDOW) {
      const oldest = routeParents.current.keys().next().value;
      if (oldest === undefined) break;
      routeParents.current.delete(oldest);
    }
  }, [route, snapshot]);

  const browserSnapshot = snapshot && route.kind === "recipe"
    ? restoreRouteParent(snapshot, route.recipeId, routeParents.current.get(route.recipeId))
    : snapshot;
  const modelCount = snapshot?.models.length ?? 0;
  const linkedRecipeCount = snapshot?.models.reduce((total, model) => total + model.recipes.length, 0) ?? 0;
  const unlinkedRecipeCount = snapshot?.unlinked_recipes.length ?? 0;
  const recipeCount = linkedRecipeCount + unlinkedRecipeCount;

  function updateCustomDocument(updater: (document: VisualRecipeDocument) => VisualRecipeDocument) {
    const next = updater(customDocument);
    setCustomDocument(next);
    setDocumentText(JSON.stringify(next, null, 2));
  }

  function updateCustomDocumentText(value: string) {
    setDocumentText(value);
    const result = parseVisualRecipeDocument(value);
    if (result.ok) {
      setCustomDocument(result.document);
      setSlug(result.document.identity.slug);
    }
  }

  async function refreshPublicRecipes() {
    if (!catalog.listPublicRecipes) {
      setPublicRecipesError("The public recipe catalog is not available in this control-plane build.");
      setPublicRecipesLoading(false);
      return;
    }
    setPublicRecipesLoading(true);
    setPublicRecipesError("");
    try {
      const result = await catalog.listPublicRecipes();
      setPublicRecipes(result.recipes);
      setPublicRecipesCommit(result.commit);
    } catch (value) {
      setPublicRecipesError(value instanceof Error ? value.message : "Unable to load the current recipe catalog");
    } finally {
      setPublicRecipesLoading(false);
    }
  }

  function openPublicImport() {
    setAuthoring("import");
    setAuthoringStatus("");
    setImportError("");
    setImportPreview(undefined);
    setPublicRecipeQuery("");
    void refreshPublicRecipes();
  }

  async function previewPublicImport(uri = importUri) {
    if (!catalog.previewPublicRecipe || !uri) return;
    setImportUri(uri);
    setImportError("");
    setImportPreview(undefined);
    setImportPreviewLoading(true);
    try {
      setImportPreview(await catalog.previewPublicRecipe(uri));
    } catch (value) {
      setImportError(value instanceof Error ? value.message : "Unable to preview import");
    } finally {
      setImportPreviewLoading(false);
    }
  }

  async function savePublicImport() {
    if (!catalog.importPublicRecipe || !importPreview) return;
    setImportError("");
    setImportSaving(true);
    try {
      await catalog.importPublicRecipe(importPreview.uri, importPreview.content_sha256);
      setAuthoringStatus("Recipe imported");
    } catch (value) {
      setImportError(value instanceof Error ? value.message : "Unable to import recipe");
    } finally {
      setImportSaving(false);
    }
  }

  return <div className="library-page">
    <header className="fleet-hero">
      <div>
        <p className="fleet-kicker">Model control</p>
        <h2 ref={heading} tabIndex={-1}>Library</h2>
      <div className="library-toolbar-actions">
        <button type="button" className="button secondary" onClick={() => { const next = defaultDocument(); setCustomDocument(next); setDocumentText(JSON.stringify(next, null, 2)); setSlug(next.identity.slug); setAuthoring("create"); setAuthoringStatus(""); }}>Create custom recipe</button>
        <button type="button" className="button secondary" onClick={openPublicImport}>Import public recipe</button>
      </div>
      {authoring === "create" && <section className="library-section library-authoring-panel" aria-label="Recipe authoring">
        <div className="library-panel-heading"><div><p className="fleet-kicker">Local recipe builder</p><h3>Create custom recipe</h3><p>Describe every part of the runtime in a guided form. Use advanced JSON only when you need a field-level escape hatch.</p></div><span className="library-panel-badge">Draft</span></div>
        <CustomRecipeForm document={customDocument} slug={slug} onSlugChange={setSlug} onChange={updateCustomDocument} />
        <details className="library-json-fallback"><summary>Advanced JSON fallback</summary><div className="library-json-fallback-content"><p>Paste or edit the complete canonical recipe document. Valid JSON updates the form above; invalid JSON is kept for correction.</p><label>Recipe document<textarea aria-label="Recipe document" rows={12} spellCheck={false} value={documentText} onChange={event => updateCustomDocumentText(event.target.value)} /></label></div></details>
        <div className="button-row"><button type="button" className="button secondary" onClick={() => { const result = parseVisualRecipeDocument(documentText); if (result.ok) { setCustomDocument(result.document); setSlug(result.document.identity.slug); } setAuthoringStatus(result.ok ? "Recipe document valid" : result.error); }}>Validate recipe</button><button type="button" className="button" disabled={!catalog.createCatalogRecipe} onClick={() => { try { const result = parseVisualRecipeDocument(documentText); if (!result.ok) { setAuthoringStatus(result.error); return; } void catalog.createCatalogRecipe?.({slug, document: result.document}).then(() => setAuthoringStatus("Recipe saved")); } catch { setAuthoringStatus("Unable to save recipe"); } }}>Save custom recipe</button></div>
        {authoringStatus && <p role="status" aria-label={authoringStatus === "Recipe saved" || authoringStatus === "Recipe imported" ? "Recipe authoring" : "Recipe validation"}>{authoringStatus}</p>}
        <button type="button" className="button secondary" onClick={() => setAuthoring(undefined)}>Close authoring</button>
      </section>}
      {authoring === "import" && <section className="library-section library-import-panel" aria-label="Public recipe import">
        <div className="library-panel-heading"><div><p className="fleet-kicker">Public recipe catalog</p><h3>Import public recipe</h3><p>Choose a reviewed recipe from the live catalog, or paste an immutable public URI. Every option is previewed before it is saved locally.</p></div><span className="library-panel-badge">Digest-bound</span></div>
        <div className="library-import-source">
          <div className="library-import-source-heading"><div><span className="library-import-eyebrow">Recommended</span><h4>Reviewed recipe library</h4></div><div className="library-import-catalog-meta"><span className="library-import-count">{publicRecipesLoading ? "Refreshing…" : `${publicRecipes.length} recipes`}</span>{publicRecipesCommit && <code title={publicRecipesCommit}>@{publicRecipesCommit.slice(0, 8)}</code>}</div></div>
          {publicRecipesLoading && publicRecipes.length === 0 && <div className="library-import-loading" role="status"><span aria-hidden="true" /><div><strong>Loading the reviewed catalog</strong><small>Resolving one immutable library snapshot…</small></div></div>}
          {publicRecipesError && <div className="library-import-error" role="alert"><div><strong>Catalog unavailable</strong><p>{publicRecipesError}</p></div><button type="button" className="button secondary" onClick={() => void refreshPublicRecipes()}>Try again</button></div>}
          {!publicRecipesLoading && publicRecipes.length > 0 && <>
            <label className="library-import-search"><span>Find a recipe</span><input type="search" aria-label="Search public recipes" value={publicRecipeQuery} onChange={event => setPublicRecipeQuery(event.target.value)} placeholder="Search model, modality, runtime, or tag…" /></label>
            <p className="library-import-helper">Showing {filteredPublicRecipes.length} of {publicRecipes.length} recipes from <code>{publicRecipesCommit.slice(0, 8)}</code>.</p>
            <div className="library-import-grid" aria-label="Default catalog recipes">
              {filteredPublicRecipes.map(recipe => <article className={`library-import-card${importUri === recipe.uri ? " selected" : ""}`} key={recipe.uri}>
                <div><span className="library-import-eyebrow">{recipe.tags.includes("candidate") ? "Candidate" : "Reviewed"}</span><h5>{recipe.title}</h5><p>{recipe.description}</p></div>
                <div className="library-import-tags" aria-label={`${recipe.title} tags`}>{recipe.tags.slice(0, 5).map(tag => <span className="library-import-tag" key={tag}>{tag}</span>)}</div>
                <button type="button" className="button secondary" aria-pressed={importUri === recipe.uri} onClick={() => void previewPublicImport(recipe.uri)}>{importUri === recipe.uri && importPreviewLoading ? "Loading preview…" : "Review recipe"}</button>
              </article>)}
            </div>
            {filteredPublicRecipes.length === 0 && <div className="library-import-empty"><strong>No matching recipes</strong><p>Try a model name, modality such as “video”, or runtime such as “vLLM”.</p></div>}
          </>}
        </div>
        <details className="library-import-manual"><summary>Import a public recipe URI</summary><div className="library-import-manual-content"><div><span className="library-import-eyebrow">Advanced</span><h4>Manual URI</h4></div><label>Public recipe URI<input aria-label="Public recipe URI" value={importUri} onChange={event => { setImportUri(event.target.value); setImportPreview(undefined); setImportError(""); }} placeholder="vonk://catalog/publisher/slug@sha256:…" /></label><div className="library-import-actions"><button type="button" className="button secondary" disabled={!catalog.previewPublicRecipe || !importUri || importPreviewLoading} onClick={() => void previewPublicImport()}>{importPreviewLoading ? "Loading preview…" : "Review URI"}</button><span>Use this for another publisher or an immutable URI you already have.</span></div></div></details>
        {importError && <p role="alert">{importError}</p>}
        {importPreview && <section className="library-import-preview" aria-label="Public recipe import preview"><div className="library-import-preview-heading"><div><span className="library-import-eyebrow">Ready for review</span><h4>{importPreview.title}</h4><p>{importPreview.publisher}/{importPreview.slug}</p></div><span className="library-import-status">Immutable</span></div><p className="library-import-description">{importPreview.description || "No description provided."}</p>{importPreview.tags.length > 0 && <div className="library-import-tags" aria-label="Recipe tags">{importPreview.tags.map(tag => <span className="library-import-tag" key={tag}>{tag}</span>)}</div>}<dl className="library-import-facts"><div><dt>Source</dt><dd>{importPreview.source === "recipe_library" ? "Reviewed recipe library" : "Public catalog"}</dd></div><div><dt>Identity</dt><dd>{importPreview.publisher}/{importPreview.slug}</dd></div></dl><p className="library-import-digest"><span>Immutable content digest</span><code>sha256:{importPreview.content_sha256}</code></p><button type="button" className="button" disabled={!catalog.importPublicRecipe || importSaving} onClick={() => void savePublicImport()}>{importSaving ? "Importing…" : "Import reviewed recipe"}</button></section>}
        {authoringStatus && <p role="status" aria-label={authoringStatus === "Recipe saved" || authoringStatus === "Recipe imported" ? "Recipe authoring" : "Recipe validation"}>{authoringStatus}</p>}
        <button type="button" className="button secondary" onClick={() => setAuthoring(undefined)}>Close import</button>
      </section>}
        <p className="fleet-introduction">Choose a model, its exact recipe, and one complete placement group before reviewing any change.</p>
      </div>
    </header>
    {snapshot && <>
      <section className="library-overview" aria-label="Library summary">
        <div className="library-stat library-stat-accent" role="group" aria-label={`${modelCount} model version${modelCount === 1 ? "" : "s"}`}><span>Model versions</span><strong>{modelCount}</strong><small>Exact immutable identities</small></div>
        <div className="library-stat" role="group" aria-label={`${recipeCount} recipes`}><span>Recipes in view</span><strong>{recipeCount}</strong><small>{paginationWindowed ? "Bounded loaded window" : "Available locally"}</small></div>
        <div className="library-stat" role="group" aria-label={`${linkedRecipeCount} linked`}><span>Linked recipes</span><strong>{linkedRecipeCount}</strong><small>Ready to choose a model</small></div>
        <div className={`library-stat${unlinkedRecipeCount > 0 ? " library-stat-warning" : ""}`} role="group" aria-label={`${unlinkedRecipeCount} needs a model version`}><span>Needs model version</span><strong>{unlinkedRecipeCount}</strong><small>{unlinkedRecipeCount > 0 ? "Review before install" : "Everything has an exact model"}</small></div>
      </section>
      <div className="library-toolbar">
        <label className="library-search">
          <span>Find a model or recipe</span>
          <input type="search" aria-label="Search Library" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search names, slugs, capabilities…" />
        </label>
        <div className="library-search-meta" aria-live="polite">
          {query.trim() ? <><span>Filtering the loaded window</span><button type="button" className="button secondary" onClick={() => setQuery("")}>Clear Library search</button></> : <span>Browse exact versions and their accepted recipes</span>}
        </div>
      </div>
    </>}
    {error && <section className="fleet-error" role="alert"><h3>Library unavailable</h3><p>{error}</p><button type="button" onClick={() => setSnapshotAttempt(value => value + 1)}>Retry Library</button></section>}
    {!error && !snapshot && <section className="fleet-loading" role="status" aria-label="Loading Library"><span className="loading-orb" aria-hidden="true"/><div><h3>Opening Library</h3><p>Loading model, recipe, and placement authority…</p></div></section>}
    {snapshot && snapshot.models.length === 0 && snapshot.unlinked_recipes.length === 0 && <section className="fleet-empty"><h3>No recipes in the Library</h3><p>Recipes will appear here after they are added to the local library authority.</p></section>}
    {browserSnapshot && (browserSnapshot.models.length > 0 || browserSnapshot.unlinked_recipes.length > 0) && <LibraryBrowser
      api={api}
      detail={detail}
      detailError={detailError}
      detailLoading={detailLoading}
      onNavigate={onNavigate}
      onRefresh={refreshDetail}
      onRetryDetail={() => setDetailAttempt(value => value + 1)}
      query={query}
      route={route}
      snapshot={browserSnapshot}
      windowed={paginationWindowed}
    />}
    {snapshot && (snapshot.next_cursor || paginationWindowed) && <div className="library-pagination">
      {paginationWindowed && <p role="status" aria-label="Bounded Library window">Showing up to {LIBRARY_RECIPE_WINDOW} recipes per list and {LIBRARY_MODEL_WINDOW} models. Earlier loaded rows leave this bounded window; selected context stays pinned. {snapshot.next_cursor ? "More server pages remain." : "No more server pages remain."}</p>}
      {snapshot.next_cursor && <button type="button" className="button secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading more recipes…" : "Load more Library recipes"}</button>}
      {paginationError && <p role="alert">{paginationError}</p>}
    </div>}
  </div>;
}

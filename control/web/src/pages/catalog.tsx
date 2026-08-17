import {useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeSummary, GlobalRecipeRevision} from "../api/types";
import {RecipeSummary} from "../components/recipe-summary";

type CatalogListApi = Pick<CatalogApi, "catalogRecipes"> & Partial<Pick<CatalogApi, "previewGlobalRecipe" | "importGlobalRecipe">>;

function object(value: unknown): Record<string, unknown> { return typeof value === "object" && value !== null ? value as Record<string, unknown> : {}; }
function gb(value: unknown): string { return `${(Number(value) / 1_000_000_000).toFixed(1)} GB`; }
function topology(value: unknown): Record<string, unknown> { return object(value); }
function roles(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.map(object) : []; }
function maximumInstalled(document: Record<string, unknown>): number {
  const fields = ["image_bytes", "artifact_bytes", "staging_bytes", "cache_bytes", "rollback_bytes", "safety_margin_bytes"];
  return Math.max(0, ...roles(topology(document.topology).roles).map(role => {
    const disk = object(object(role.resources).disk);
    return fields.reduce((total, field) => total + Number(disk[field] ?? 0), 0);
  }));
}
function maximumMemory(document: Record<string, unknown>): number {
  return Math.max(0, ...roles(topology(document.topology).roles).map(role => Number(object(object(role.resources).memory).startup_peak_bytes ?? 0)));
}

export function CatalogPage({api}: {api: CatalogListApi}) {
  const [recipes, setRecipes] = useState<CatalogRecipeSummary[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [uri, setUri] = useState("");
  const [preview, setPreview] = useState<GlobalRecipeRevision | null>(null);
  const [message, setMessage] = useState("");
  useEffect(() => {
    let active = true;
    void api.catalogRecipes().then(result => { if (active) setRecipes(result.recipes); }).catch(value => { if (active) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load recipes"); });
    return () => { active = false; };
  }, [api]);
  async function review() {
    if (!api.previewGlobalRecipe) return;
    setError(""); setMessage(""); setPreview(null);
    try { setPreview(await api.previewGlobalRecipe(uri)); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to review global recipe"); }
  }
  async function importExact() {
    if (!preview || !api.importGlobalRecipe) return;
    setError("");
    try {
      const imported = await api.importGlobalRecipe(uri, preview.content_sha256);
      setRecipes(current => [imported, ...current.filter(item => item.recipe_id !== imported.recipe_id)]);
      setMessage(`${imported.title} is now in local PostgreSQL and available offline.`);
      setPreview(null);
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to import global recipe"); }
  }
  const metadata = object(preview?.document.metadata);
  const build = object(preview?.document.build);
  const context = object(build.context);
  const previewTopology = topology(preview?.document.topology);
  const installed = maximumInstalled(preview?.document ?? {});
  const memory = maximumMemory(preview?.document ?? {});
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleRecipes = recipes.filter(recipe => normalizedQuery.length === 0 || `${recipe.title} ${recipe.slug} ${recipe.execution_harness} ${recipe.runtime_distribution} ${recipe.topology_name}`.toLocaleLowerCase().includes(normalizedQuery));
  const resolvedCount = recipes.filter(recipe => recipe.lifecycle === "resolved").length;
  const distributedCount = recipes.filter(recipe => recipe.node_count > 1).length;
  return <>
    <header className="catalog-hero">
      <div><p className="fleet-kicker">Recipe authoring</p><h2>Recipe catalog</h2><p className="fleet-introduction">Local PostgreSQL is authoritative. Shape exact model versions into reproducible builds, placements, and runs.</p></div>
      <div className="catalog-hero-actions"><a className="button secondary" href="/catalog/import/workload_run">Import WorkloadRun</a><a className="button" href="/catalog/new">Create local recipe</a></div>
    </header>
    <section className="catalog-overview" aria-label="Catalog summary">
      <div className="catalog-stat catalog-stat-accent" role="group" aria-label={`${recipes.length} recipes`}><span>Total recipes</span><strong>{recipes.length}</strong><small>Local and imported revisions</small></div>
      <div className="catalog-stat" role="group" aria-label={`${resolvedCount} resolved`}><span>Resolved</span><strong>{resolvedCount}</strong><small>Ready for exact review</small></div>
      <div className={`catalog-stat${distributedCount > 0 ? " catalog-stat-distributed" : ""}`} role="group" aria-label={`${distributedCount} distributed`}><span>Distributed</span><strong>{distributedCount}</strong><small>Multi-node topologies</small></div>
      <div className="catalog-stat" role="group" aria-label={`${recipes.length - resolvedCount} drafts`}><span>Drafts</span><strong>{recipes.length - resolvedCount}</strong><small>Still being authored</small></div>
    </section>
    <div className="catalog-toolbar">
      <label className="catalog-search"><span>Find a recipe</span><input type="search" aria-label="Search catalog" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search title, harness, runtime, topology…" /></label>
      <div className="catalog-search-meta" aria-live="polite">{normalizedQuery ? <><span>{visibleRecipes.length} matching recipe{visibleRecipes.length === 1 ? "" : "s"}</span><button type="button" className="button secondary" onClick={() => setQuery("")}>Clear catalog search</button></> : <span>Exact revisions stay visible offline</span>}</div>
    </div>
    {error && <p role="alert">{error}</p>}{!error && recipes.length === 0 && <p role="status">No recipes yet.</p>}
    {!error && recipes.length > 0 && visibleRecipes.length === 0 && <p role="status">No recipes match this search.</p>}
    {message && <p role="status">{message}</p>}
    {api.previewGlobalRecipe && <section className="confirmation" aria-labelledby="global-import-heading"><h3 id="global-import-heading">Import from vonkforge.ai</h3><p>Paste the immutable URI from a public recipe. Review its exact build source, weights, sizing, and one topology before creating a durable local copy.</p><label>Immutable vonkforge.ai URI<input value={uri} onChange={event => setUri(event.target.value)} placeholder={`vonk://catalog/vonk/model@sha256:${"0".repeat(64)}`}/></label><button type="button" onClick={() => void review()}>Review global recipe</button></section>}
    {preview && <section className="confirmation" aria-labelledby="global-review-heading"><h3 id="global-review-heading">Review {String(metadata.title ?? preview.slug)}</h3><p><code>{preview.publisher}/{preview.slug}@sha256:{preview.content_sha256}</code></p><dl className="evidence-grid compact"><div><dt>Global revision</dt><dd>{preview.revision_number}</dd></div><div><dt>Source</dt><dd>sha256:{String(context.sha256 ?? "missing")}</dd></div><div><dt>Maximum disk</dt><dd>{gb(installed)} disk / node</dd></div><div><dt>Maximum memory</dt><dd>{gb(memory)} RAM / node</dd></div><div><dt>Topology</dt><dd>{String(previewTopology.name ?? "missing")} · {Number(previewTopology.node_count ?? 0)} node(s)</dd></div></dl><button type="button" onClick={() => void importExact()}>Import exact revision</button></section>}
    <div className="recipe-grid">{visibleRecipes.map(recipe => <RecipeSummary key={recipe.recipe_id} recipe={recipe}/>)}</div>
  </>;
}

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
  return <>
    <div className="page-heading"><div><h2>Recipe catalog</h2><p>Local PostgreSQL is authoritative. Recipes remain available when vonkforge.ai or Git is unavailable.</p></div><div className="actions"><a className="button" href="/catalog/import/workload_run">Import WorkloadRun</a><a className="button" href="/catalog/new">Create local recipe</a></div></div>
    {error && <p role="alert">{error}</p>}{!error && recipes.length === 0 && <p role="status">No recipes yet.</p>}
    {message && <p role="status">{message}</p>}
    {api.previewGlobalRecipe && <section className="confirmation" aria-labelledby="global-import-heading"><h3 id="global-import-heading">Import from vonkforge.ai</h3><p>Paste the immutable URI from a public recipe. Review its exact build source, weights, sizing, and one topology before creating a durable local copy.</p><label>Immutable vonkforge.ai URI<input value={uri} onChange={event => setUri(event.target.value)} placeholder={`vonk://catalog/vonk/model@sha256:${"0".repeat(64)}`}/></label><button type="button" onClick={() => void review()}>Review global recipe</button></section>}
    {preview && <section className="confirmation" aria-labelledby="global-review-heading"><h3 id="global-review-heading">Review {String(metadata.title ?? preview.slug)}</h3><p><code>{preview.publisher}/{preview.slug}@sha256:{preview.content_sha256}</code></p><dl className="evidence-grid compact"><div><dt>Global revision</dt><dd>{preview.revision_number}</dd></div><div><dt>Source</dt><dd>sha256:{String(context.sha256 ?? "missing")}</dd></div><div><dt>Maximum disk</dt><dd>{gb(installed)} disk / node</dd></div><div><dt>Maximum memory</dt><dd>{gb(memory)} RAM / node</dd></div><div><dt>Topology</dt><dd>{String(previewTopology.name ?? "missing")} · {Number(previewTopology.node_count ?? 0)} node(s)</dd></div></dl><button type="button" onClick={() => void importExact()}>Import exact revision</button></section>}
    <div className="recipe-grid">{recipes.map(recipe => <RecipeSummary key={recipe.recipe_id} recipe={recipe}/>)}</div>
  </>;
}

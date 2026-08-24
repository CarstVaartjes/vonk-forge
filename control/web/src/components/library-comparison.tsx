import {useEffect, useMemo, useState} from "react";
import type {LibraryApi, LibraryRecipeDetail, LibraryRecipeSummary} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {humanizeIdentifier, TechnicalDetails} from "./library-technical-details";

const diskFields = ["image_bytes", "artifact_bytes", "staging_bytes", "cache_bytes", "rollback_bytes", "safety_margin_bytes"] as const;

function topologyDisk(detail: LibraryRecipeDetail | undefined): number | undefined {
  return detail?.topology?.roles.reduce((total, role) => total + role.count * diskFields.reduce((subtotal, field) => subtotal + role.disk[field], 0), 0);
}

function topologyMemory(detail: LibraryRecipeDetail | undefined): number | undefined {
  return detail?.topology?.roles.reduce((total, role) => total + role.count * role.memory.startup_peak_bytes, 0);
}

function status(recipe: LibraryRecipeSummary): {label: string; tone: string} {
  if (recipe.runs.some(run => run.state === "running" && run.healthy)) return {label: "Running", tone: "healthy"};
  if (recipe.runs.some(run => run.state === "running")) return {label: "Running · attention", tone: "warning"};
  if (recipe.installations.some(installation => installation.state === "installed")) return {label: "Installed", tone: "healthy"};
  if (recipe.selected_revision?.lifecycle === "resolved") return {label: "Ready", tone: "neutral"};
  return {label: "Needs review", tone: "warning"};
}

function ResourceBar({label, value, maximum}: {label: string; value?: number; maximum: number}) {
  const width = value === undefined || value === 0 || maximum === 0 ? 0 : Math.max(5, Math.round(value / maximum * 100));
  return <div className="comparison-resource" aria-label={`${label}: ${value === undefined ? "Not declared" : formatBytes(value)}`}>
    <span>{value === undefined ? "Not declared" : formatBytes(value)}</span>
    <span className="comparison-resource-track" aria-hidden="true"><span style={{width: `${width}%`}}/></span>
  </div>;
}

function TopologyGraphic({detail, summary}: {detail?: LibraryRecipeDetail; summary: LibraryRecipeSummary}) {
  const count = detail?.topology?.node_count;
  return <div className="comparison-topology">
    <div className="comparison-sparks" aria-hidden="true">
      {Array.from({length: Math.min(count ?? 0, 8)}, (_, index) => <span key={index}/>) }
    </div>
    <span>{count ? `${count} Spark${count === 1 ? "" : "s"}` : "Spark count unavailable"}</span>
    <small>{detail?.topology ? `${humanizeIdentifier(detail.topology.name)} · ${humanizeIdentifier(detail.topology.mode)}` : summary.topology_name ? humanizeIdentifier(summary.topology_name) : "No valid topology"}</small>
  </div>;
}

export function LibraryComparison({api, recipes, selectedIds, onToggle}: {
  api: LibraryApi;
  recipes: LibraryRecipeSummary[];
  selectedIds: string[];
  onToggle(recipeId: string): void;
}) {
  const [details, setDetails] = useState<Record<string, LibraryRecipeDetail>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const selected = useMemo(() => selectedIds.flatMap(id => {
    const recipe = recipes.find(item => item.recipe_id === id);
    return recipe ? [recipe] : [];
  }), [recipes, selectedIds]);

  useEffect(() => {
    const controller = new AbortController();
    for (const recipe of selected) {
      if (details[recipe.recipe_id]) continue;
      void api.libraryRecipe(recipe.recipe_id, controller.signal)
        .then(detail => {
          if (!controller.signal.aborted) setDetails(current => ({...current, [recipe.recipe_id]: detail}));
        })
        .catch(value => {
          if (!controller.signal.aborted) setErrors(current => ({...current, [recipe.recipe_id]: value instanceof Error ? value.message.slice(0, 160) : "Unable to load comparison detail"}));
        });
    }
    return () => controller.abort();
  }, [api, selected]);

  if (selected.length === 0) return <section className="library-compare-empty" aria-label="Recipe comparison">
    <div className="library-empty-visual" aria-hidden="true"><span/><span/><span/></div>
    <h3>Choose recipes to compare</h3>
    <p>Select up to three recipes. Resource envelopes, Spark topology, capabilities, and local status will line up here.</p>
  </section>;

  const memoryMaximum = Math.max(0, ...selected.map(recipe => topologyMemory(details[recipe.recipe_id]) ?? 0));
  const diskMaximum = Math.max(0, ...selected.map(recipe => topologyDisk(details[recipe.recipe_id]) ?? 0));

  return <section className="library-comparison" aria-label="Recipe comparison">
    <div className="library-comparison-heading"><div><p className="fleet-kicker">Side-by-side</p><h3>Recipe comparison</h3></div><p>{selected.length} of 3 selected</p></div>
    <div className="library-comparison-scroll" tabIndex={0} aria-label="Scrollable recipe comparison table">
      <table>
        <thead><tr><th scope="col">Compare</th>{selected.map(recipe => <th scope="col" key={recipe.recipe_id}>
          <strong>{recipe.title}</strong>
          <small>{recipe.description}</small>
          <button type="button" className="button secondary" onClick={() => onToggle(recipe.recipe_id)}>Remove <span className="visually-hidden">{recipe.title} from comparison</span></button>
        </th>)}</tr></thead>
        <tbody>
          <tr><th scope="row">Local status</th>{selected.map(recipe => { const current = status(recipe); return <td key={recipe.recipe_id}><span className={`comparison-status comparison-status-${current.tone}`}>{current.label}</span><small>{recipe.installation_total_count} installed · {recipe.run_total_count} active</small></td>; })}</tr>
          <tr><th scope="row">Spark topology</th>{selected.map(recipe => <td key={recipe.recipe_id}>{errors[recipe.recipe_id] ? <span role="alert">{errors[recipe.recipe_id]}</span> : details[recipe.recipe_id] ? <TopologyGraphic detail={details[recipe.recipe_id]} summary={recipe}/> : <span role="status">Loading topology…</span>}</td>)}</tr>
          <tr><th scope="row">Startup memory</th>{selected.map(recipe => <td key={recipe.recipe_id}><ResourceBar label="Startup memory" value={topologyMemory(details[recipe.recipe_id])} maximum={memoryMaximum}/></td>)}</tr>
          <tr><th scope="row">Disk envelope</th>{selected.map(recipe => <td key={recipe.recipe_id}><ResourceBar label="Disk envelope" value={topologyDisk(details[recipe.recipe_id])} maximum={diskMaximum}/></td>)}</tr>
          <tr><th scope="row">Capabilities</th>{selected.map(recipe => <td key={recipe.recipe_id}><div className="comparison-capabilities">{recipe.capabilities.length ? recipe.capabilities.map(capability => <span key={capability}>{humanizeIdentifier(capability)}</span>) : <span>Not declared</span>}</div></td>)}</tr>
          <tr><th scope="row">Technical details</th>{selected.map(recipe => <td key={recipe.recipe_id}><TechnicalDetails compact items={[
            {label: "Recipe ID", value: recipe.recipe_id},
            {label: "Recipe slug", value: recipe.slug},
            {label: "Revision ID", value: recipe.selected_revision?.id ?? ""},
            {label: "Content digest", value: recipe.selected_revision?.content_sha256 ?? ""},
          ]}/></td>)}</tr>
        </tbody>
      </table>
    </div>
  </section>;
}

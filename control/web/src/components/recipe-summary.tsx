import type {CatalogRecipeSummary} from "../api/types";

const originLabels = {local: "Local", workload_run: "Imported from WorkloadRun", global: "Downloaded from vonkforge.ai"} as const;

function bytes(value: number): string {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)} GB`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)} MB`;
  return `${value} B`;
}

export function RecipeSummary({recipe}: {recipe: CatalogRecipeSummary}) {
  const nodes = `${recipe.node_count} node${recipe.node_count === 1 ? "" : "s"}`;
  return <article className="recipe-card" aria-label={`${recipe.title} recipe`}>
    <div className="recipe-card-heading"><div><span className={`origin origin-${recipe.origin}`}>{originLabels[recipe.origin]}</span><h3>{recipe.title}</h3></div><span className="status">{recipe.lifecycle}</span></div>
    <p className="recipe-card-identity"><span>{recipe.execution_harness}</span><span>{recipe.topology_name}</span></p>
    <dl className="recipe-facts">
      <div><dt>Runtime</dt><dd>{recipe.runtime_distribution}</dd></div><div><dt>Topology</dt><dd>{recipe.topology_name} · {nodes}</dd></div>
      <div><dt>Install</dt><dd>up to {bytes(recipe.maximum_installed_bytes_per_node)} disk / node</dd></div>
      <div><dt>Run</dt><dd>up to {bytes(recipe.maximum_runtime_memory_bytes_per_node)} RAM / node</dd></div>
    </dl>
    <p className="digest">Source sha256:{recipe.source_bundle_sha256.slice(0, 12)}…</p>
    <p className="digest">{recipe.content_sha256 ? `sha256:${recipe.content_sha256.slice(0, 12)}…` : `Draft revision ${recipe.revision_number}`}</p>
    <a href={`/catalog/${encodeURIComponent(recipe.recipe_id)}`}>Open recipe</a>
  </article>;
}

import {useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeRevision, ControlApi, RecipeMappingPlan} from "../api/types";

type MappingApi = Pick<CatalogApi, "catalogRecipe" | "previewRecipeMapping" | "createRecipeMapping"> & Pick<ControlApi, "agents">;
type Topology = {name: string; node_count: number; mode: string};

export function ClusterMappingPage({api, recipeId}: {api: MappingApi; recipeId: string}) {
  const [recipe, setRecipe] = useState<CatalogRecipeRevision | null>(null);
  const [nodes, setNodes] = useState<Array<{node_id: string; stale: boolean; state: string}>>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [plan, setPlan] = useState<RecipeMappingPlan | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const topology = (recipe?.document.topology ?? null) as Topology | null;
  useEffect(() => {
    let active = true;
    void Promise.all([api.catalogRecipe(recipeId), api.agents()]).then(([loaded, fleet]) => {
      if (!active) return;
      setRecipe(loaded); setNodes(fleet.agents.filter(node => !node.stale && node.state === "active"));
    }).catch(value => { if (active) setError(value instanceof Error ? value.message : "Unable to load mapping workflow"); });
    return () => { active = false; };
  }, [api, recipeId]);
  function toggle(nodeId: string) { setPlan(null); setSelected(current => current.includes(nodeId) ? current.filter(value => value !== nodeId) : [...current, nodeId]); }
  async function preview() {
    if (!recipe || !topology) return;
    setError(""); setMessage("");
    try { setPlan(await api.previewRecipeMapping(recipe.id, selected)); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to preview mapping"); }
  }
  async function create() {
    if (!plan) return;
    setError("");
    try { const mapping = await api.createRecipeMapping(plan); setMessage(`Cluster mapping ${mapping.mapping_id} generation ${mapping.generation} created.`); setPlan(null); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to create mapping"); }
  }
  return <>
    <div className="page-heading"><div><h2>Map recipe to cluster</h2><p>The immutable recipe defines one topology; this mapping chooses its GPU nodes and preserves the defined rank and role order.</p></div><a className="button" href={`/catalog/${recipeId}`}>Back to recipe</a></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <section className="confirmation"><p><strong>Topology:</strong> {topology?.name ?? "unavailable"} ({topology?.mode ?? "unknown"})</p><fieldset><legend>Select exactly {topology?.node_count ?? 0} online GPU nodes</legend>{nodes.map(node => <label key={node.node_id}><input type="checkbox" checked={selected.includes(node.node_id)} onChange={() => toggle(node.node_id)}/><code>{node.node_id}</code></label>)}</fieldset><button type="button" disabled={!topology || selected.length !== topology.node_count} onClick={() => void preview()}>Preview ranks, roles, and capacity identity</button></section>
    {plan && <section className="confirmation"><h3>Immutable placement preview</h3><p><code>{plan.placement_digest}</code></p><table><thead><tr><th>Rank</th><th>Role</th><th>GPU node</th><th>Endpoint</th></tr></thead><tbody>{plan.nodes.map(node => <tr key={node.node_id}><td>{node.rank}</td><td>{node.role}</td><td><code>{node.node_id}</code></td><td>{node.endpoint_owner ? "yes" : "no"}</td></tr>)}</tbody></table><button type="button" onClick={() => void create()}>Create cluster mapping</button></section>}
  </>;
}

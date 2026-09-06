import {useState} from "react";
import type {ControlApi, LibraryRecipeDetail as RecipeDetail, LibrarySnapshot, RunSwitchOperation, RunSwitchPlan, RunSwitchPreviewRequest} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {ArtifactJobWorkspace} from "./artifact-job-workspace";
import {LibraryProfileComposer} from "./library-profile-composer";
import {LibraryPlacement} from "./library-placement";
import {LibraryRunSwitchProgress} from "./library-run-switch-progress";
import type {LibraryPlacementGroup} from "./library-action-types";
import {useLibraryNodeName} from "./library-node-names";
import {LibraryRecipeFit} from "./library-recipe-fit";
import {LibraryRecipeVisual} from "./library-recipe-visual";
import "./library-recipe-detail.css";

export function LibraryRecipeAuthority({api, detail, snapshot, onRefresh: _onRefresh, onBusyChange}: {api: ControlApi; detail: RecipeDetail; snapshot?: LibrarySnapshot; onRefresh?: (signal: AbortSignal) => Promise<void>; onBusyChange?(busy: boolean): void}) {
  const documents = detail.model_documents;
  const files = documents.flatMap(model => model.model_document.files);
  const [runOperation, setRunOperation] = useState<RunSwitchOperation>();
  const [runPlan, setRunPlan] = useState<RunSwitchPlan>();
  const [runError, setRunError] = useState("");
  const [runStarting, setRunStarting] = useState(false);
  const nodeName = useLibraryNodeName();
  const modelVersionSha256 = documents[0]?.selection.model.content_sha256;
  const alias = detail.definition.interfaces.find(item => item.adapter === "openai")?.model_aliases?.[0] ?? detail.recipe.slug;
  const nodeNames = Object.fromEntries(detail.placement.flatMap(placement => placement.recommendations.flatMap(group => group.nodes.map(node => [node.node_id, nodeName(node.node_id)] as const))));
  async function run(group: LibraryPlacementGroup) {
    if (runStarting || !modelVersionSha256) return;
    setRunStarting(true); setRunError(""); setRunPlan(undefined);
    const requestKey = crypto.randomUUID();
    const nodes = group.nodes.map(node => ({node_id: node.node_id, rank: node.rank, role: node.role, endpoint_owner: node.endpoint_owner}));
    const input: RunSwitchPreviewRequest = {schema_version: 2, model_version_sha256: modelVersionSha256, recipe_revision_id: detail.recipe.recipe_revision_id, spark_group: {nodes}, alias, action: "run", retention: "retain-cached", invocation: {origin: "web.library", context: {recipe_id: detail.recipe.recipe_id, node_ids: nodes.map(node => node.node_id).join(",")}}};
    try {
      const plan = await api.previewRecipeRunSwitch(input); setRunPlan(plan);
      if (!plan.allowed || plan.blockers.length) { setRunError("The Controller needs attention before this Recipe can run."); return; }
      setRunOperation(await api.applyRecipeRunSwitch({...input, plan_digest: plan.plan_digest, request_key: requestKey}));
    } catch (value) { setRunError(value instanceof Error ? value.message.slice(0, 256) : "The Controller could not start this Recipe."); } finally { setRunStarting(false); }
  }
  const policy = snapshot?.freshness_policy ?? {inventory_fresh_seconds: 300, telemetry_delayed_seconds: 20, telemetry_live_seconds: 6};
  const runPlacementDetail: RecipeDetail = {
    ...detail,
    placement: detail.placement.map(placement => ({
      ...placement,
      recommendations: placement.recommendations.map(group => ({
        ...group,
        preview_targets: group.preview_targets.filter(target => target.kind === "run"),
      })),
      rejected_groups: placement.rejected_groups.map(group => ({
        ...group,
        preview_targets: group.preview_targets.filter(target => target.kind === "run"),
      })),
    })),
  };
  return <article className="library-recipe-detail" aria-labelledby="recipe-detail-heading"><header className="library-detail-heading"><div><span>Exact Recipe</span><h2 id="recipe-detail-heading">{detail.recipe.title}</h2><p>{detail.recipe.description}</p></div><span className="library-schema-badge">Schema {detail.schema_version}</span></header><LibraryRecipeFit detail={detail}/><section className="library-section" aria-label="Models used by Recipe"><header><h3>Ordered Model inputs</h3><span>{documents.length} Model{documents.length === 1 ? "" : "s"} · {files.length} files</span></header><ol className="library-model-detail-list">{documents.map((item, index) => <li key={`${item.model_document.identity.publisher}/${item.model_document.identity.slug}`}><span className="library-order">{index + 1}</span><div><strong>{item.model_document.identity.model.title}</strong><small>{item.model_document.identity.publisher}/{item.model_document.identity.slug} · {item.selection.files.length} Recipe mount{item.selection.files.length === 1 ? "" : "s"}</small><ul>{item.model_document.files.map(file => <li key={file.id}>{file.path} · {formatBytes(file.size_bytes)} · sha256:{file.sha256.slice(0, 12)}…</li>)}</ul></div></li>)}</ol></section><ArtifactJobWorkspace api={api} detail={detail} onBusyChange={onBusyChange}/><LibraryProfileComposer api={api} detail={detail}/><LibraryPlacement detail={runPlacementDetail} policy={policy} actionsDisabled={runStarting || Boolean(runOperation)} onRun={run}/>{runStarting && <p role="status">Preparing the exact Model and Recipe on the selected Sparks…</p>}{runError && <p role="alert">{runError}</p>}{runPlan && runPlan.blockers.length > 0 && <ul aria-label="Run blockers">{runPlan.blockers.map(blocker => <li key={`${blocker.code}:${blocker.detail}`}>{blocker.detail}</li>)}</ul>}{runOperation && <LibraryRunSwitchProgress api={api} nodeNames={nodeNames} onChange={setRunOperation} operation={runOperation} title={detail.recipe.title}/>}<LibraryRecipeVisual document={detail.definition} modelDocuments={documents}/><section className="library-section" aria-label="Operational state"><header><h3>Controller state</h3><span>{detail.operational_state.installations.length} installations · {detail.operational_state.runs.length} runs</span></header><p>{detail.operational_state.runs.length ? "This Recipe has observed runs on the fleet." : "No active run is reported for this Recipe."}</p></section></article>;
}

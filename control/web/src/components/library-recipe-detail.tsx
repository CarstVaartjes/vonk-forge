import {useCallback, useEffect, useRef, useState} from "react";
import type {ControlApi, LibraryRecipeDetail, LibrarySnapshot, PublicRecipe, RunSwitchOperation, RunSwitchPlan, RunSwitchPreviewRequest} from "../api/types";
import type {LibraryApi, LibraryOperation} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {StatusPill} from "./status-pill";
import {LibraryPlacement, primaryPlacementRecommendation} from "./library-placement";
import {LibraryReasons} from "./library-reasons";
import {LibraryActionDialog} from "./library-action-dialog";
import {actionName} from "./library-action-types";
import type {LibraryActionName, LibraryActionReview, LibraryActionTarget, LibraryPlacementGroup} from "./library-action-types";
import {LibraryOperationProgress, operationSettled} from "./library-operation-progress";
import {LibraryProfileComposer} from "./library-profile-composer";
import {LibraryRecipeAdvanced} from "./library-recipe-advanced";
import {LibraryRecipeVisual} from "./library-recipe-visual";
import {ArtifactJobWorkspace} from "./artifact-job-workspace";
import {LibraryRecipeFit} from "./library-recipe-fit";
import {humanizeIdentifier, TechnicalDetails} from "./library-technical-details";
import {LibraryRunSwitchProgress} from "./library-run-switch-progress";
import type {RunSwitchApi} from "./library-run-switch-progress";
import "./library-recipe-detail.css";

type Topology = NonNullable<LibraryRecipeDetail["topology"]>;

const diskFields = ["image_bytes", "artifact_bytes", "staging_bytes", "cache_bytes", "rollback_bytes", "safety_margin_bytes"] as const;

function topologyRanks(topology: Topology) {
  let rank = 0;
  return topology.roles.flatMap(role => Array.from({length: role.count}, () => ({...role, rank: rank++})));
}

function topologyDisk(topology: Topology): number {
  return topology.roles.reduce((total, role) => total + role.count * diskFields.reduce((subtotal, field) => subtotal + role.disk[field], 0), 0);
}

function topologyMemory(topology: Topology): number {
  return topology.roles.reduce((total, role) => total + role.count * role.memory.startup_peak_bytes, 0);
}

function operationTone(state: string) {
  if (["succeeded", "installed", "ready", "running", "published"].includes(state)) return "healthy" as const;
  if (["failed", "lost", "partial", "stale"].includes(state)) return "danger" as const;
  return "warning" as const;
}

function operationLabel(kind: string, state: string): string {
  return `${kind} ${state}`.replace(/^./, letter => letter.toUpperCase());
}

function lifecycleStage(label: string, items: Array<{state: string}>, completeStates: readonly string[], reachedLabel = "Complete") {
  const states = [...new Set(items.map(item => item.state))];
  if (states.length === 0) return {label, state: "Not started", detail: "No authority record", tone: "idle"};
  if (states.some(state => ["failed", "lost", "partial", "stale"].includes(state))) {
    return {label, state: "Attention", detail: states.map(humanizeIdentifier).join(" · "), tone: "danger"};
  }
  if (states.every(state => completeStates.includes(state))) {
    return {label, state: reachedLabel, detail: states.map(humanizeIdentifier).join(" · "), tone: "healthy"};
  }
  return {label, state: "In progress", detail: states.map(humanizeIdentifier).join(" · "), tone: "warning"};
}

function nextActionCopy(name: LibraryActionName): {description: string; title: string} {
  if (name === "Build") return {title: "Build the recipe image", description: "Create the immutable runtime image before installation. The server preview will verify the builder, source bundle, and exact build digest."};
  if (name === "Mapping") return {title: "Map the complete Spark group", description: "Bind every declared rank to a compatible Spark group before distributing or installing the recipe."};
  if (name === "Distribute") return {title: "Distribute the exact image", description: "Copy the verified image to every mapped Spark before installation."};
  if (name === "Install") return {title: "Install on the selected Sparks", description: "Review disk admission and install this immutable revision on every rank in the group."};
  if (name === "Load") return {title: "Load and publish the model", description: "Review live memory admission, then start every rank and publish the endpoint."};
  return {title: `Review ${name.toLocaleLowerCase()}`, description: "Review the current server-authoritative plan before changing lifecycle state."};
}

function useNarrowViewport(query: string): boolean {
  const [matches, setMatches] = useState(() => typeof window !== "undefined" && window.matchMedia?.(query).matches === true);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}

function runAlias(value: string): string {
  return value.toLocaleLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 128);
}

export function LibraryRecipeAuthority({api, catalogRecipe, detail, modelVersionSha256, nodeNames = {}, onBusyChange, onRefresh, policy, preferredNodeId, runApi}: {
  api: LibraryApi;
  catalogRecipe?: PublicRecipe;
  detail: LibraryRecipeDetail;
  modelVersionSha256?: string;
  nodeNames?: Record<string, string>;
  onBusyChange?(busy: boolean): void;
  onRefresh(signal: AbortSignal): Promise<void>;
  policy: LibrarySnapshot["freshness_policy"];
  preferredNodeId?: string;
  runApi?: RunSwitchApi & Pick<ControlApi, "previewRecipeRunSwitch" | "applyRecipeRunSwitch">;
}) {
  const [review, setReview] = useState<LibraryActionReview>();
  const [operation, setOperation] = useState<LibraryOperation>();
  const [runOperation, setRunOperation] = useState<RunSwitchOperation>();
  const [runPlan, setRunPlan] = useState<RunSwitchPlan>();
  const [runError, setRunError] = useState("");
  const [runStarting, setRunStarting] = useState(false);
  const [runRequestKey, setRunRequestKey] = useState("");
  const [runRetry, setRunRetry] = useState(0);
  const [runGroup, setRunGroup] = useState<LibraryPlacementGroup>();
  const [operationName, setOperationName] = useState<LibraryActionName>("Load");
  const narrowViewport = useNarrowViewport("(max-width: 520px)");
  const [mobileQualificationOpen, setMobileQualificationOpen] = useState(false);
  const canonicalPreviewKey = [
    detail.recipe.recipe_id,
    detail.selected_revision?.id ?? "",
    detail.selected_revision?.content_sha256 ?? "",
    JSON.stringify(detail.visual_recipe),
  ].join(":");
  const [preview, setPreview] = useState({document: detail.visual_recipe, canonicalKey: canonicalPreviewKey, local: false});
  const trigger = useRef<HTMLButtonElement | null>(null);
  const visual = preview.canonicalKey === canonicalPreviewKey ? preview.document : detail.visual_recipe;
  const localPreview = visual !== null && preview.canonicalKey === canonicalPreviewKey && preview.local;
  const revision = detail.selected_revision;
  const alias = detail.visual_recipe?.interfaces.find(item => item.adapter === "openai")?.model_aliases?.[0] ?? detail.recipe.slug;
  useEffect(() => {
    setPreview(current => current.canonicalKey === canonicalPreviewKey
      ? current
      : {document: detail.visual_recipe, canonicalKey: canonicalPreviewKey, local: false});
  }, [canonicalPreviewKey, detail.visual_recipe]);
  const closeReview = useCallback(() => {
    setReview(undefined);
    const returnTo = trigger.current;
    queueMicrotask(() => returnTo?.focus());
  }, []);
  const openReview = useCallback((target: LibraryActionTarget, returnTo: HTMLButtonElement, evidence?: LibraryPlacementGroup) => {
    trigger.current = returnTo;
    setReview({evidence, target});
  }, []);
  const onApplied = useCallback((next: LibraryOperation, name: LibraryActionName) => {
    setOperationName(name);
    setOperation(next);
  }, []);
  const actionBlocked = operation !== undefined && (!operationSettled(operation.state) || ["partial", "failed", "cancelled", "canceled", "lost"].includes(operation.state));
  const runActive = Boolean(runOperation && !["succeeded", "failed", "cancelled", "partially_succeeded", "blocked"].includes(runOperation.state));
  const lifecycleStages = [
    lifecycleStage("Build", detail.operational_state.builds, ["succeeded"]),
    lifecycleStage("Map", detail.operational_state.mappings, ["ready"]),
    lifecycleStage("Install", detail.operational_state.installations, ["installed"]),
    lifecycleStage("Run", detail.operational_state.runs, ["running", "published"], "Active"),
  ];
  const activeRun = detail.operational_state.runs.some(run => ["running", "published"].includes(run.state));
  const placementRecommendation = activeRun ? undefined : primaryPlacementRecommendation(detail);
  const recommendedName = placementRecommendation ? actionName(placementRecommendation.target) : undefined;
  const recommendationCopy = recommendedName ? nextActionCopy(recommendedName) : undefined;
  const directRunAvailable = recommendedName === "Load" && Boolean(runApi);

  useEffect(() => {
    onBusyChange?.(runStarting || runActive);
    return () => onBusyChange?.(false);
  }, [onBusyChange, runActive, runStarting]);

  useEffect(() => {
    setRunOperation(undefined);
    setRunPlan(undefined);
    setRunError("");
    setRunRequestKey("");
    setRunGroup(undefined);
  }, [detail.recipe.recipe_id, detail.selected_revision?.id]);

  async function runGroupNow(group: LibraryPlacementGroup, retry = false) {
    if (runStarting || runActive) return;
    if (!runApi) {
      setRunError("Run is unavailable until the Controller run/switch contract is connected.");
      return;
    }
    const revision = detail.selected_revision;
    if (!revision?.id || revision.lifecycle !== "resolved") {
      setRunError("This recipe has no resolved revision to run.");
      return;
    }
    if (!modelVersionSha256 || !/^[0-9a-f]{64}$/.test(modelVersionSha256)) {
      setRunError("The exact model version digest is not reported. The Controller cannot run this recipe safely yet.");
      return;
    }
    const selectedNodes = group.nodes.map(node => ({node_id: node.node_id, rank: node.rank, role: node.role, endpoint_owner: node.endpoint_owner}));
    if (selectedNodes.length === 0 || selectedNodes.filter(node => node.endpoint_owner).length !== 1) {
      setRunError("The selected Spark group is incomplete. Choose a complete group with one endpoint owner.");
      return;
    }
    const nextRequestKey = retry && runRequestKey ? runRequestKey : crypto.randomUUID();
    const input: RunSwitchPreviewRequest = {
      schema_version: 2,
      model_version_sha256: modelVersionSha256,
      recipe_revision_id: revision.id,
      spark_group: {nodes: selectedNodes},
      alias: runAlias(alias) || "model",
      action: "run",
      retention: "retain-cached",
      invocation: {origin: "web.library", context: {recipe_id: detail.recipe.recipe_id, node_ids: selectedNodes.map(node => node.node_id).join(",")}},
    };
    setRunGroup(group);
    setRunRequestKey(nextRequestKey);
    setRunError("");
    setRunPlan(undefined);
    setRunStarting(true);
    try {
      const plan = await runApi.previewRecipeRunSwitch(input);
      setRunPlan(plan);
      if (!plan.allowed || plan.blockers.length > 0) {
        setRunError("The Controller needs attention before this model can run.");
        return;
      }
      const operation = await runApi.applyRecipeRunSwitch({...input, plan_digest: plan.plan_digest, request_key: nextRequestKey});
      setRunOperation(operation);
    } catch (value) {
      setRunError(value instanceof Error ? value.message.slice(0, 256) : "The Controller could not start this model.");
    } finally {
      setRunStarting(false);
    }
  }

  function retryRun() {
    if (runGroup) void runGroupNow(runGroup, true);
  }
  return <div className="recipe-authority" role="region" aria-label={`${detail.recipe.title} recipe authority`}>
    <header className="recipe-authority-hero">
      <div>
        <p className="fleet-kicker">{visual ? humanizeIdentifier(visual.identity.publisher) : humanizeIdentifier(detail.recipe.source_kind)} recipe</p>
        <strong className="recipe-authority-title">{visual?.metadata.title ?? detail.recipe.title}</strong>
        <p>{visual?.metadata.description ?? detail.recipe.description}</p>
        {visual && <p className="recipe-metadata-tags">{visual.metadata.tags.length > 0 ? visual.metadata.tags.join(" · ") : "No tags declared"}</p>}
        <TechnicalDetails compact items={[
          {label: "Recipe ID", value: detail.recipe.recipe_id},
          {label: "Recipe slug", value: detail.recipe.slug},
          {label: "Revision ID", value: revision?.id ?? ""},
          {label: "Content digest", value: revision?.content_sha256 ?? ""},
        ]}/>
      </div>
      <div className="recipe-authority-statuses">
        {localPreview && <StatusPill tone="warning">Local preview · not saved</StatusPill>}
        <StatusPill tone={revision?.lifecycle === "resolved" ? "healthy" : "warning"}>{revision ? `${revision.lifecycle === "resolved" ? "Immutable" : revision.lifecycle} revision ${revision.revision_number}` : "No valid revision"}</StatusPill>
      </div>
    </header>
    <LibraryRecipeFit catalogRecipe={catalogRecipe} detail={detail}/>
    <section className={`recipe-next-action${activeRun ? " is-running" : placementRecommendation ? "" : " is-blocked"}`} aria-label="Recommended next action">
      <div>
        <p className="fleet-kicker">Recommended next step</p>
        <h4>{activeRun ? "Model is running" : recommendationCopy?.title ?? "Resolve placement readiness"}</h4>
        <p>{activeRun
          ? "No lifecycle change is required. Use Fleet for live node, route, and workload health."
          : recommendationCopy?.description ?? "No complete Spark group currently exposes an authorized lifecycle action. Review placement blockers and evidence below."}</p>
      </div>
      {activeRun
        ? <a className="button secondary" href="/fleet">Open Fleet</a>
        : placementRecommendation && recommendedName && placementRecommendation.groupCount === 1
          ? <button type="button" className="button" disabled={actionBlocked || directRunAvailable && (runStarting || runActive || !modelVersionSha256)} onClick={event => directRunAvailable ? void runGroupNow(placementRecommendation.group) : openReview(placementRecommendation.target, event.currentTarget, placementRecommendation.group)}>{directRunAvailable ? "Run" : `Review ${recommendedName}`}</button>
          : <button type="button" className="button secondary" onClick={() => document.getElementById("recipe-placement")?.scrollIntoView({block: "start"})}>{placementRecommendation ? "Choose a Spark group" : "Review placement"}</button>}
    </section>
    <ArtifactJobWorkspace api={api} detail={detail} onBusyChange={onBusyChange}/>
    <LibraryProfileComposer api={api} detail={detail} preferredNodeId={preferredNodeId}/>
    <details className="recipe-qualification-disclosure" open={!narrowViewport || mobileQualificationOpen} onToggle={event => {
      if (narrowViewport && event.currentTarget.open !== mobileQualificationOpen) setMobileQualificationOpen(event.currentTarget.open);
    }}>
      <summary><span><strong>Technical qualification</strong><small>Lifecycle, placement, runtime and topology</small></span><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m6 9 6 6 6-6" strokeLinecap="round" strokeLinejoin="round"/></svg></summary>
      <div className="recipe-qualification-body">
    <section className="library-section library-primary-control" aria-label="Lifecycle overview">
      <div className="section-heading"><div><p className="fleet-kicker">Current authority</p><h4>Lifecycle overview</h4></div><span className="identity-note">Build · map · install · run</span></div>
      <ol className="lifecycle-track" aria-label="Recipe lifecycle stages">
        {lifecycleStages.map(stage => <li className={`lifecycle-stage lifecycle-stage-${stage.tone}`} key={stage.label} aria-label={`${stage.label}: ${stage.state}. ${stage.detail}`}>
          <span className="lifecycle-marker" aria-hidden="true"/><div><strong>{stage.label}</strong><span>{stage.state}</span><small>{stage.detail}</small></div>
        </li>)}
      </ol>
      <div className="operation-summary" aria-label="Available lifecycle actions">
        {detail.operational_state.installations.map((installation, index) => <div className="operation-item" key={installation.installation_id}>
          <StatusPill tone={operationTone(installation.state)}>{operationLabel(`installation ${index + 1}`, installation.state)}</StatusPill>
          {installation.state !== "uninstalled" && <button type="button" disabled={actionBlocked} onClick={event => openReview({kind: "uninstall", installationId: installation.installation_id}, event.currentTarget)}>Review removal of installation {index + 1}</button>}
          <TechnicalDetails compact items={[{label: "Installation ID", value: installation.installation_id}]}/>
        </div>)}
        {detail.operational_state.runs.map((run, index) => <div className="operation-item" key={run.run_id}>
          <StatusPill tone={operationTone(run.state)}>{operationLabel(`run ${index + 1}`, run.state)}</StatusPill>
          {!['stopped'].includes(run.state) && <button type="button" disabled={actionBlocked} onClick={event => openReview({kind: "stop", runId: run.run_id}, event.currentTarget)}>Review stop of run {index + 1}</button>}
          <TechnicalDetails compact items={[{label: "Run ID", value: run.run_id}, {label: "Installation ID", value: run.installation_id}]}/>
        </div>)}
        {Object.values(detail.operational_state).every(items => items.length === 0) && <p>No operation history for this revision.</p>}
      </div>
    </section>
    {operation && <LibraryOperationProgress api={api} name={operationName} onChange={setOperation} onRefresh={onRefresh} operation={operation}/>}
    {runStarting && <section className="library-run-switch-progress is-starting" aria-label="Run progress" aria-live="polite"><strong>Checking Controller plan…</strong><span>Preparing the exact model and runtime for the selected Spark group.</span></section>}
    {runError && <section className="library-run-switch-error" role="alert"><span>{runError}</span>{runGroup && <button type="button" className="button secondary" onClick={retryRun}>Retry run</button>}</section>}
    {runPlan && !runOperation && !runStarting && <section className="library-run-switch-plan" aria-label="Run issues"><header><strong>Run needs attention</strong><span>The Controller did not dispatch this request.</span></header>{runPlan.blockers.length > 0 && <ul>{runPlan.blockers.map(reason => <li key={`${reason.code}:${reason.detail}`}><strong>{reason.code}</strong><span>{reason.detail}</span></li>)}</ul>}{runPlan.warnings.length > 0 && <details><summary>{runPlan.warnings.length} warning{runPlan.warnings.length === 1 ? "" : "s"}</summary><ul>{runPlan.warnings.map(reason => <li key={`${reason.code}:${reason.detail}`}>{reason.detail}</li>)}</ul></details>}<button type="button" className="button secondary" onClick={() => runGroup && void runGroupNow(runGroup, true)}>Recheck run</button></section>}
    {runOperation && runApi && <LibraryRunSwitchProgress api={runApi} nodeNames={nodeNames} onChange={setRunOperation} onRefresh={onRefresh ? () => void onRefresh(new AbortController().signal) : undefined} onRetry={retryRun} operation={runOperation} title={detail.recipe.title}/>}
    <LibraryPlacement actionsDisabled={actionBlocked || runStarting || runActive} detail={detail} onReview={openReview} onRun={runApi ? runGroupNow : undefined} policy={policy} preferredNodeId={preferredNodeId}/>
    {visual && <>
      <LibraryRecipeVisual document={visual}/>
      <section className="library-section" aria-label="Topology and resources">
        <div className="section-heading"><div><p className="fleet-kicker">Declared topology</p><h4>Topology and ranks</h4></div></div>
        {detail.topology && <article className="topology-card">
          <h5>{humanizeIdentifier(detail.topology.name)}</h5><p>{detail.topology.node_count} Sparks · {humanizeIdentifier(detail.topology.mode)}</p>
          <div className="resource-totals"><strong>{formatBytes(topologyMemory(detail.topology))} startup memory total</strong><strong>{formatBytes(topologyDisk(detail.topology))} disk envelope total</strong></div>
          <figure className="topology-diagram" aria-label={`${detail.topology.node_count}-Spark ${humanizeIdentifier(detail.topology.mode)} topology over ${humanizeIdentifier(detail.topology.fabric.connectivity)} fabric`}>
            <figcaption><span className="topology-fabric-badge">{humanizeIdentifier(detail.topology.fabric.connectivity)} fabric</span><span>{detail.topology.fabric.minimum_bandwidth_mbps.toLocaleString()} Mbps minimum · {humanizeIdentifier(detail.topology.parallelism.backend)}</span></figcaption>
            <ol className="topology-rank-diagram" aria-label="Topology ranks" tabIndex={0}>{topologyRanks(detail.topology).map(role => <li key={`${role.rank}:${role.name}`}>
              <span className="topology-rank-node" aria-hidden="true">{role.rank}</span><div><strong>Rank {role.rank} · {humanizeIdentifier(role.name)}{role.endpoint_owner ? " · endpoint owner" : ""}</strong><span>{formatBytes(role.memory.startup_peak_bytes)} startup memory · {formatBytes(diskFields.reduce((total, field) => total + role.disk[field], 0))} disk envelope</span></div>
            </li>)}</ol>
          </figure>
          <p className="topology-fabric">Start: {detail.topology.start_order.map(humanizeIdentifier).join(" → ")} · Stop: {detail.topology.stop_order.map(humanizeIdentifier).join(" → ")}</p>
        </article>}
      </section>
    </>}
    <LibraryReasons reasons={detail.reasons}/>
    {detail.visual_recipe && <LibraryRecipeAdvanced
      document={detail.visual_recipe}
      onValidDocument={document => setPreview({document, canonicalKey: canonicalPreviewKey, local: true})}
      resetToken={canonicalPreviewKey}
    />}
      </div>
    </details>
    {review && <LibraryActionDialog alias={alias} api={api} evidence={review.evidence} onApplied={onApplied} onBusyChange={onBusyChange} onClose={closeReview} onRefresh={onRefresh} policy={policy} target={review.target}/>}
  </div>;
}

import {useCallback, useEffect, useRef, useState} from "react";
import type {LibraryRecipeDetail, LibrarySnapshot} from "../api/types";
import type {LibraryApi, LibraryOperation} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {StatusPill} from "./status-pill";
import {LibraryPlacement} from "./library-placement";
import {LibraryReasons} from "./library-reasons";
import {LibraryActionDialog} from "./library-action-dialog";
import type {LibraryActionName, LibraryActionReview, LibraryActionTarget, LibraryPlacementGroup} from "./library-action-types";
import {LibraryOperationProgress, operationSettled} from "./library-operation-progress";
import {LibraryRecipeAdvanced} from "./library-recipe-advanced";
import {LibraryRecipeVisual} from "./library-recipe-visual";
import {humanizeIdentifier, TechnicalDetails} from "./library-technical-details";
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

export function LibraryRecipeAuthority({api, detail, onBusyChange, onRefresh, policy}: {
  api: LibraryApi;
  detail: LibraryRecipeDetail;
  onBusyChange?(busy: boolean): void;
  onRefresh(signal: AbortSignal): Promise<void>;
  policy: LibrarySnapshot["freshness_policy"];
}) {
  const [review, setReview] = useState<LibraryActionReview>();
  const [operation, setOperation] = useState<LibraryOperation>();
  const [operationName, setOperationName] = useState<LibraryActionName>("Load");
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
  const lifecycleStages = [
    lifecycleStage("Build", detail.operational_state.builds, ["succeeded"]),
    lifecycleStage("Map", detail.operational_state.mappings, ["ready"]),
    lifecycleStage("Install", detail.operational_state.installations, ["installed"]),
    lifecycleStage("Run", detail.operational_state.runs, ["running", "published"], "Active"),
  ];
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
    <LibraryPlacement actionsDisabled={actionBlocked} detail={detail} onReview={openReview} policy={policy}/>
    {visual && <>
      <LibraryRecipeVisual document={visual}/>
      <section className="library-section" aria-label="Topology and resources">
        <div className="section-heading"><div><p className="fleet-kicker">Declared topology</p><h4>Topology and ranks</h4></div></div>
        {detail.topology && <article className="topology-card">
          <h5>{humanizeIdentifier(detail.topology.name)}</h5><p>{detail.topology.node_count} Sparks · {humanizeIdentifier(detail.topology.mode)}</p>
          <div className="resource-totals"><strong>{formatBytes(topologyMemory(detail.topology))} startup memory total</strong><strong>{formatBytes(topologyDisk(detail.topology))} disk envelope total</strong></div>
          <figure className="topology-diagram" aria-label={`${detail.topology.node_count}-Spark ${humanizeIdentifier(detail.topology.mode)} topology over ${humanizeIdentifier(detail.topology.fabric.connectivity)} fabric`}>
            <figcaption><span className="topology-fabric-badge">{humanizeIdentifier(detail.topology.fabric.connectivity)} fabric</span><span>{detail.topology.fabric.minimum_bandwidth_mbps.toLocaleString()} Mbps minimum · {humanizeIdentifier(detail.topology.parallelism.backend)}</span></figcaption>
            <ol className="topology-rank-diagram" aria-label="Topology ranks">{topologyRanks(detail.topology).map(role => <li key={`${role.rank}:${role.name}`}>
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
    {review && <LibraryActionDialog alias={alias} api={api} evidence={review.evidence} onApplied={onApplied} onBusyChange={onBusyChange} onClose={closeReview} onRefresh={onRefresh} policy={policy} target={review.target}/>}
  </div>;
}

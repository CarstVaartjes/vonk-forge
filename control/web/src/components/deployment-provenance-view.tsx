import {useEffect, useState} from "react";
import type {components} from "../api/generated";
import {CopyButton} from "./copy-button";
import {StatusPill} from "./status-pill";
import "./deployment-provenance-view.css";

type Provenance = components["schemas"]["DeploymentProvenance"];
type Evidence = components["schemas"]["EvidenceAge"];
type ProvenanceApi = {deploymentProvenance(): Promise<Provenance>};
const labels: Record<string, string> = {
  repository: "Repository", publication: "Publication", controller_deployment: "Controller deployment",
  repository_not_published: "Repository differs from publication", publication_not_deployed: "Publication differs from deployment",
  observed: "Observed", unknown: "Unknown", accepted: "Physically accepted", failed: "Acceptance failed",
  not_qualified: "Not physically qualified", identity_mismatch: "Acceptance belongs to another identity",
  match: "Identities agree", mismatch: "Identity mismatch",
};
const label = (value: string) => labels[value] ?? value.replaceAll("_", " ");
const short = (value?: string | null) => value ? value.replace(/^sha256:/, "").slice(0, 12) : "Unknown";

function Age({evidence}: {evidence: Evidence}) {
  const age = evidence.age_seconds == null ? "Age unknown" : evidence.age_seconds < 60 ? `${evidence.age_seconds}s ago` : evidence.age_seconds < 3600 ? `${Math.floor(evidence.age_seconds / 60)}m ago` : `${Math.floor(evidence.age_seconds / 3600)}h ago`;
  return <small className="provenance-evidence" title={evidence.observed_at ?? undefined}>{evidence.freshness === "stale" ? "Stale · " : ""}{age} · {evidence.source}</small>;
}

export function DeploymentProvenanceView({api}: {api: ProvenanceApi}) {
  const [snapshot, setSnapshot] = useState<Provenance>();
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let current = true;
    setError(false);
    setSnapshot(undefined);
    void api.deploymentProvenance().then(value => { if (current) setSnapshot(value); }, () => { if (current) setError(true); });
    return () => { current = false; };
  }, [api, refresh]);
  return <section className="deployment-provenance" aria-labelledby="deployment-provenance-heading">
    <div className="provenance-heading"><h2 id="deployment-provenance-heading">Deployment provenance</h2><button className="secondary-button" type="button" onClick={() => setRefresh(value => value + 1)}>Refresh evidence</button>{snapshot && <CopyButton label="provenance JSON" value={JSON.stringify(snapshot, null, 2)}/>}</div>
    <p>Exact artifacts and the evidence behind each deployment boundary. Publication and readiness do not prove physical acceptance.</p>
    {error ? <p role="alert">Deployment evidence could not be loaded. Refresh evidence to try again.</p> : !snapshot ? <p role="status">Loading deployment evidence…</p> : <>
      <dl className="provenance-boundaries">{snapshot.platform.map(boundary => <div key={boundary.boundary}>
        <dt>{label(boundary.boundary)}</dt><dd><StatusPill tone={boundary.state.includes("not_") ? "warning" : "neutral"}>{label(boundary.state)}</StatusPill><span title={boundary.source_commit ?? undefined}>Commit {short(boundary.source_commit)}</span><span title={boundary.image_digest ?? undefined}>Image {short(boundary.image_digest)}</span><Age evidence={boundary.evidence}/></dd>
      </div>)}<div><dt>Recipe-library authority</dt><dd>{snapshot.recipe_library.repository ?? "Unknown"}<span title={snapshot.recipe_library.source_commit ?? undefined}>Commit {short(snapshot.recipe_library.source_commit)}</span><span>{label(snapshot.recipe_library.state)}</span><Age evidence={snapshot.recipe_library.evidence}/></dd></div></dl>
      <h3>Sparks</h3>
      {snapshot.agents.length === 0 ? <p>No Spark deployment evidence recorded.</p> : <ul className="provenance-agents">{snapshot.agents.map(agent => <li key={agent.node_id}><strong>{agent.display_name}</strong><StatusPill tone={agent.connectivity === "offline" ? "warning" : "neutral"}>{agent.connectivity === "offline" ? "Offline · last observed" : label(agent.connectivity)}</StatusPill><span>Agent {agent.semantic_version ?? "unknown"}</span><span title={agent.binary_sha256 ?? undefined}>Binary {short(agent.binary_sha256)}</span><span title={agent.package_sha256 ?? undefined}>Package {short(agent.package_sha256)}</span><Age evidence={agent.evidence}/></li>)}</ul>}
      <h3>Installed recipes and runtime load</h3>
      {snapshot.workloads.length === 0 ? <p>No active installations recorded.</p> : snapshot.workloads.map(workload => <article className="provenance-workload" key={workload.installation_id}>
        <div className="provenance-heading"><h4>{workload.recipe_publisher}/{workload.recipe_slug}</h4><span>Installation: {workload.installation_state}</span><span>Runtime: {workload.run_state ?? "Not loaded"}</span><StatusPill tone={workload.rank_agreement === "mismatch" ? "warning" : "neutral"}>{label(workload.rank_agreement)}</StatusPill></div>
        <p>Recipe <code title={workload.recipe_content_sha256}>{short(workload.recipe_content_sha256)}</code> · image <code title={workload.image_digest}>{short(workload.image_digest)}</code> · mapping {workload.mapping_generation} · run generation {workload.run_generation ?? "unknown"}{workload.mapping_agreement === "mismatch" && " · Mapping changed"}</p>
        <ul className="provenance-ranks">{workload.ranks.map(rank => <li key={rank.node_id}><strong>Rank {rank.rank} · {snapshot.agents.find(agent => agent.node_id === rank.node_id)?.display_name ?? rank.node_id}</strong><span>{rank.role} · installed: {rank.installation_state} · runtime: {rank.runtime_state}</span><StatusPill tone={rank.identity_agreement === "mismatch" ? "warning" : "neutral"}>{label(rank.identity_agreement)}</StatusPill><Age evidence={rank.runtime_evidence}/></li>)}</ul>
        <div className="provenance-acceptance"><StatusPill tone={workload.physical_acceptance.state === "accepted" ? "healthy" : workload.physical_acceptance.state === "failed" ? "danger" : "neutral"}>{label(workload.physical_acceptance.state)}</StatusPill><Age evidence={workload.physical_acceptance.evidence}/></div>
        <details><summary>Immutable artifact identities</summary><dl className="provenance-identities"><dt>Installation</dt><dd>{workload.installation_id}</dd><dt>Run</dt><dd>{workload.run_id ?? "Unknown"}</dd><dt>Recipe revision</dt><dd>{workload.recipe_revision_id} · revision {workload.recipe_revision_number} · {workload.recipe_content_sha256}</dd><dt>Source bundle</dt><dd>{workload.source_bundle_sha256 ?? "Unknown"}</dd><dt>Build input</dt><dd>{workload.build_input_sha256 ?? "Unknown"}</dd><dt>Runtime image</dt><dd>{workload.image_digest}</dd>{workload.models.map(model => <div key={model.selection_id}><dt>{model.publisher}/{model.slug}</dt><dd>{model.repository} · revision {model.revision} · content {model.content_sha256}</dd></div>)}</dl></details>
      </article>)}
      <details><summary>Complete evidence JSON</summary><pre className="provenance-json">{JSON.stringify(snapshot, null, 2)}</pre></details>
    </>}
  </section>;
}

import type {LibraryRecipeDetail} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {exactIdentity, friendlyModelName, humanizeIdentifier, TechnicalDetails} from "./library-technical-details";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;

function bytes(value: number): string {
  return formatBytes(value);
}

function values(items: readonly string[], empty: string): string {
  return items.length > 0 ? items.join(" · ") : empty;
}

export function LibraryRecipeVisual({document}: {document: VisualRecipeDocument}) {
  return <>
    <section className="library-section recipe-identity" aria-label="Recipe identity">
      <div className="section-heading"><div><p className="fleet-kicker">Runtime chain</p><h4>What will run</h4></div><span className="identity-note">Exact identities available on demand</span></div>
      <div className="recipe-identity-grid">
        <div className="recipe-identity-card"><span className="identity-index">01</span><span>Model version</span><strong>{friendlyModelName(document.model)}</strong><small>Immutable model artifact and format</small><TechnicalDetails compact items={[{label: "Exact model identity", value: exactIdentity(document.model)}]}/></div>
        <div className="recipe-identity-card"><span className="identity-index">02</span><span>Execution harness</span><strong>{humanizeIdentifier(document.execution.harness.slug)}</strong><small>Lifecycle compiler and interface contract</small><TechnicalDetails compact items={[{label: "Exact harness identity", value: exactIdentity(document.execution.harness)}]}/></div>
        <div className="recipe-identity-card"><span className="identity-index">03</span><span>Runtime distribution</span><strong>{humanizeIdentifier(document.runtime.distribution.slug)}</strong><small>Signed image and dependency boundary</small><TechnicalDetails compact items={[{label: "Exact runtime identity", value: exactIdentity(document.runtime.distribution)}]}/></div>
      </div>
    </section>

    <section className="library-section visual-document-section" aria-label="Build and artifacts">
      <div className="section-heading"><div><p className="fleet-kicker">Visual document</p><h4>Build and artifacts</h4></div></div>
      <dl className="visual-field-grid">
        <div><dt>Schema version</dt><dd> {document.schema_version}</dd></div>
        <div><dt>Dockerfile</dt><dd> {document.build.dockerfile}</dd></div>
        <div><dt>Target stage</dt><dd> {document.build.target ?? "Final stage"}</dd></div>
        <div><dt>Platform</dt><dd> {document.build.platform}</dd></div>
        <div><dt>Network mode</dt><dd> {document.build.network_mode}</dd></div>
        <div><dt>Network hosts</dt><dd> {values(document.build.network_hosts, "None")}</dd></div>
        <div><dt>Context expected</dt><dd> {bytes(document.build.context.expected_bytes)}</dd></div>
        <div><dt>Context media type</dt><dd> {document.build.context.media_type}</dd></div>
        <div><dt>Download</dt><dd> {bytes(document.build.download_bytes)}</dd></div>
        <div><dt>Temporary storage</dt><dd> {bytes(document.build.temporary_bytes)}</dd></div>
        <div><dt>Build memory</dt><dd> {bytes(document.build.memory_bytes)}</dd></div>
        <div><dt>Build CPU</dt><dd> {document.build.cpu_cores.toLocaleString("en-US")} cores</dd></div>
        <div><dt>Process limit</dt><dd> {document.build.processes.toLocaleString("en-US")}</dd></div>
        <div><dt>Build capabilities</dt><dd> {document.build.capabilities.length ? document.build.capabilities.join(", ") : "None"}</dd></div>
        <div><dt>Build format</dt><dd> {document.build.options.format.toUpperCase()} · {document.build.options.jobs.toLocaleString("en-US")} parallel {document.build.options.jobs === 1 ? "stage" : "stages"}</dd></div>
        <div><dt>Timeout</dt><dd> {document.build.timeout_seconds.toLocaleString("en-US")} seconds</dd></div>
      </dl>
      <TechnicalDetails items={[{label: "Context digest", value: `sha256:${document.build.context.sha256}`}]}/>
      <div className="visual-artifacts">
        {document.artifacts.length === 0 && <p>No artifacts declared.</p>}
        {document.artifacts.map((artifact, index) => <article key={`${artifact.id}:${index}`} aria-label={`Artifact ${humanizeIdentifier(artifact.id)}`}>
          <strong>{humanizeIdentifier(artifact.id)}</strong>
          <dl className="visual-field-grid">
            <div><dt>Kind</dt><dd> {artifact.kind}</dd></div>
            <div><dt>Repository</dt><dd> {artifact.repository}</dd></div>
            <div><dt>Download</dt><dd> {bytes(artifact.download_bytes)}</dd></div>
            <div><dt>Installed</dt><dd> {bytes(artifact.installed_bytes)}</dd></div>
            <div><dt>Roles</dt><dd> {values(artifact.roles, "None")}</dd></div>
          </dl>
          <TechnicalDetails compact items={[{label: "Artifact ID", value: artifact.id}, {label: "Artifact revision", value: artifact.revision}]}/>
        </article>)}
      </div>
    </section>

    <section className="library-section visual-document-section" aria-label="Runtime contract">
      <div className="section-heading"><div><p className="fleet-kicker">Strict v1 runtime</p><h4>Runtime contract</h4></div></div>
      <dl className="visual-field-grid">
        <div><dt>Entrypoint</dt><dd> {values(document.runtime.entrypoint, "None")}</dd></div>
        <div><dt>Pre-start phases</dt><dd> {document.runtime.lifecycle_pre_start_count}</dd></div>
        <div><dt>Post-stop phases</dt><dd> {document.runtime.lifecycle_post_stop_count}</dd></div>
        <div><dt>Stop timeout</dt><dd> {document.runtime.stop_timeout_seconds} seconds</dd></div>
      </dl>
      {document.interfaces.map((item, index) => <dl className="visual-field-grid" key={`${item.adapter}:${index}`}>
        <div><dt>Interface adapter</dt><dd> {item.adapter}</dd></div>
        <div><dt>Port</dt><dd> {item.port?.toLocaleString("en-US") ?? "Not declared"}</dd></div>
        <div><dt>Model aliases</dt><dd> {values(item.model_aliases ?? [], "None")}</dd></div>
        <div><dt>Health path</dt><dd> {item.health_path ?? "Not declared"}</dd></div>
        <div><dt>Job path</dt><dd> {item.path ?? "Not declared"}</dd></div>
      </dl>)}
    </section>

    <section className="library-section evidence-columns" aria-label="Provenance and validation">
      <div><h4>Provenance</h4><p>{document.provenance.source_kind} · {document.provenance.source_reference ?? "No external reference"}</p><p>{values(document.provenance.attribution, "No attribution declared")}</p></div>
      <div><h4>Validation</h4><p>{values(document.validation.checks, "No checks declared")}</p><p>{document.validation.benchmark_count} benchmarks</p></div>
    </section>
  </>;
}

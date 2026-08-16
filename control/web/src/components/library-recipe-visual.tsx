import type {LibraryRecipeDetail} from "../api/types";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;

function bytes(value: number): string {
  return `${value.toLocaleString("en-US")} bytes`;
}

function values(items: readonly string[], empty: string): string {
  return items.length > 0 ? items.join(" · ") : empty;
}

function identity(value: {publisher: string; slug: string; content_sha256: string}): string {
  return `${value.publisher}/${value.slug}@${value.content_sha256}`;
}

export function LibraryRecipeVisual({document}: {document: VisualRecipeDocument}) {
  return <>
    <section className="library-section recipe-identity" aria-label="Recipe identity">
      <div className="section-heading"><div><p className="fleet-kicker">Exact runtime chain</p><h4>What will run</h4></div><span className="identity-note">Every identity is digest-bound</span></div>
      <div className="recipe-identity-grid">
        <div className="recipe-identity-card"><span className="identity-index">01</span><span>Model version</span><strong>{identity(document.model)}</strong><small>Immutable model artifact and format</small></div>
        <div className="recipe-identity-card"><span className="identity-index">02</span><span>Execution harness</span><strong>{identity(document.execution.harness)}</strong><small>Lifecycle compiler and interface contract</small></div>
        <div className="recipe-identity-card"><span className="identity-index">03</span><span>Runtime distribution</span><strong>{identity(document.runtime.distribution)}</strong><small>Signed image and dependency boundary</small></div>
        <div className="recipe-identity-card recipe-identity-card-muted"><span className="identity-index">04</span><span>Patch bundle</span><strong>{document.execution.patch_bundle ? identity(document.execution.patch_bundle) : "None"}</strong><small>{document.execution.patch_bundle ? "Targeted immutable source changes" : "No recipe-local patch applied"}</small></div>
      </div>
    </section>

    <section className="library-section visual-document-section" aria-label="Build and artifacts">
      <div className="section-heading"><div><p className="fleet-kicker">Visual document</p><h4>Build and artifacts</h4></div></div>
      <dl className="visual-field-grid">
        <div><dt>Schema version</dt><dd> {document.schema_version}</dd></div>
        <div><dt>Dockerfile</dt><dd> {document.build.dockerfile}</dd></div>
        <div><dt>Platform</dt><dd> {document.build.platform}</dd></div>
        <div><dt>Network mode</dt><dd> {document.build.network_mode}</dd></div>
        <div><dt>Network hosts</dt><dd> {values(document.build.network_hosts, "None")}</dd></div>
        <div><dt>Context expected</dt><dd> {bytes(document.build.context.expected_bytes)}</dd></div>
        <div><dt>Context media type</dt><dd> {document.build.context.media_type}</dd></div>
        <div><dt>Download</dt><dd> {bytes(document.build.download_bytes)}</dd></div>
        <div><dt>Temporary storage</dt><dd> {bytes(document.build.temporary_bytes)}</dd></div>
        <div><dt>Build memory</dt><dd> {bytes(document.build.memory_bytes)}</dd></div>
        <div><dt>Timeout</dt><dd> {document.build.timeout_seconds.toLocaleString("en-US")} seconds</dd></div>
      </dl>
      <p className="visual-digest"><span>Context digest</span><code>sha256:{document.build.context.sha256}</code></p>
      <div className="visual-artifacts">
        {document.artifacts.length === 0 && <p>No artifacts declared.</p>}
        {document.artifacts.map((artifact, index) => <article key={`${artifact.id}:${index}`} aria-label={`Artifact ${artifact.id}`}>
          <strong>{artifact.id}</strong>
          <dl className="visual-field-grid">
            <div><dt>Kind</dt><dd> {artifact.kind}</dd></div>
            <div><dt>Repository</dt><dd> {artifact.repository}</dd></div>
            <div><dt>Revision</dt><dd> {artifact.revision}</dd></div>
            <div><dt>Download</dt><dd> {bytes(artifact.download_bytes)}</dd></div>
            <div><dt>Installed</dt><dd> {bytes(artifact.installed_bytes)}</dd></div>
            <div><dt>Roles</dt><dd> {values(artifact.roles, "None")}</dd></div>
          </dl>
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

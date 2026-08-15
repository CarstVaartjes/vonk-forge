import {type FormEvent, useEffect, useState} from "react";
import type {CatalogApi, CatalogRecipeDocument, CatalogRecipeRevision} from "../api/types";
import {makeSourceBundle} from "../lib/source-bundle";

type EditorApi = Pick<CatalogApi, "createCatalogRecipe"> & Partial<CatalogApi>;
type Fields = Record<string, string>;

const zeroDigest = "0".repeat(64);
const starterDockerfile = `# Replace the placeholder digest with the verified linux/arm64 digest.
FROM ghcr.io/vllm-project/vllm-openai@sha256:${zeroDigest}
LABEL ai.vonkforge.runtime-interface="v1"
USER 65532:65532
`;
const initial: Fields = {
  publisher: "local", slug: "", title: "", description: "", tags: "text",
  repository: "", revision: "", modelVersion: "", modelDigest: zeroDigest,
  executionHarness: "vllm-openai", harnessDigest: zeroDigest, runtimeDistribution: "python-312-cuda", runtimeDigest: zeroDigest,
  artifactBytes: "1", entrypoint: "vllm\nserve\n/models",
  alias: "model", port: "8000", nodeCount: "1", imageBytes: "5000000000",
  stagingBytes: "8000000000", memoryBytes: "80000000000", systemReserveBytes: "8000000000",
  buildMemoryBytes: "8000000000", buildTemporaryBytes: "12000000000", buildDownloadBytes: "1",
};

function list(value: string): string[] { return value.split(",").map(item => item.trim()).filter(Boolean); }
function lines(value: string): string[] { return value.split("\n").map(item => item.trim()).filter(Boolean); }
function positive(value: string, label: string): number {
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < 1) throw new Error(`${label} must be positive whole bytes`);
  return result;
}

function recipeDocument(fields: Fields, sourceSha256: string, archiveBytes: number): CatalogRecipeDocument {
  const nodes = positive(fields.nodeCount, "Node count");
  const artifactBytes = positive(fields.artifactBytes, "Artifact size");
  const imageBytes = positive(fields.imageBytes, "Image size");
  const stagingBytes = positive(fields.stagingBytes, "Staging size");
  const memoryBytes = positive(fields.memoryBytes, "Runtime memory");
  const reserveBytes = positive(fields.systemReserveBytes, "System reserve");
  return {
    schema_version: 1,
    identity: {publisher: fields.publisher, slug: fields.slug},
    metadata: {title: fields.title, description: fields.description, tags: list(fields.tags)},
    model: {kind: "model-version", publisher: fields.publisher, slug: fields.modelVersion || fields.slug, content_sha256: fields.modelDigest},
    execution: {harness: {kind: "execution-harness", publisher: fields.publisher, slug: fields.executionHarness, content_sha256: fields.harnessDigest}, patch_bundle: null},
    build: {
      context: {sha256: sourceSha256, expected_bytes: archiveBytes, media_type: "application/vnd.vonk-forge.source-bundle.v1+tar"},
      dockerfile: "Dockerfile", platform: "linux/arm64", arguments: [], network: {mode: "none", hosts: []},
      resources: {download_bytes: positive(fields.buildDownloadBytes, "Build download"), temporary_bytes: positive(fields.buildTemporaryBytes, "Build temporary size"), memory_bytes: positive(fields.buildMemoryBytes, "Build memory"), timeout_seconds: 3600},
    },
    artifacts: [{id: "weights", kind: "huggingface.snapshot", repository: fields.repository, revision: fields.revision, download_bytes: artifactBytes, installed_bytes: artifactBytes, mount: {target: "/models", read_only: true}, roles: ["entrypoint"]}],
    runtime: {
      distribution: {kind: "runtime-distribution", publisher: fields.publisher, slug: fields.runtimeDistribution, content_sha256: fields.runtimeDigest}, entrypoint: lines(fields.entrypoint), arguments: [], environment: [],
      security: {devices: ["nvidia.com/gpu=all"], capabilities: [], host_network: false, privileged: false, user: "65532:65532", mounts: [{source: "model", target: "/models", read_only: true}, {source: "state", target: "/state", read_only: false}]},
      lifecycle: {pre_start: [], post_stop: [], stop_timeout_seconds: 30},
    },
    topology: {
      name: nodes === 1 ? "solo" : `${nodes}-node`, mode: nodes === 1 ? "single" : "tensor_parallel", node_count: nodes,
      parallelism: {tensor: nodes, pipeline: 1, data: 1, backend: nodes === 1 ? "local" : "nccl"},
      roles: [{name: "entrypoint", count: nodes, endpoint_owner: true, artifacts: ["weights"], resources: {disk: {image_bytes: imageBytes, artifact_bytes: artifactBytes, staging_bytes: stagingBytes, cache_bytes: 1, rollback_bytes: 0, safety_margin_bytes: 10000000000}, memory: {kind: "unified", startup_peak_bytes: memoryBytes, steady_state_bytes: memoryBytes, runtime_growth_bytes: 1, system_reserve_bytes: reserveBytes}}}],
      fabric: {connectivity: nodes === 1 ? "none" : "connected", minimum_bandwidth_mbps: nodes === 1 ? 0 : 10000}, start_order: ["entrypoint"], stop_order: ["entrypoint"],
    },
    interfaces: [{adapter: "openai", port: positive(fields.port, "Endpoint port"), model_aliases: list(fields.alias), health_path: "/v1/models"}],
    validation: {validators: [{interface: "openai", checks: ["container.started", "endpoint.healthy", "inference.completed"]}], benchmarks: []},
    provenance: {source_kind: "local", source_reference: null, attribution: []},
  };
}

export function RecipeEditorPage({api, recipeId}: {api: EditorApi; recipeId?: string}) {
  const [fields, setFields] = useState(initial);
  const [dockerfile, setDockerfile] = useState(starterDockerfile);
  const [compose, setCompose] = useState("");
  const [recipe, setRecipe] = useState<CatalogRecipeRevision | null>(null);
  const [documentText, setDocumentText] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [confirmResolve, setConfirmResolve] = useState(false);
  const [targetPublisher, setTargetPublisher] = useState("vonk");
  useEffect(() => {
    if (!recipeId || !api.catalogRecipe) return;
    let active = true;
    void api.catalogRecipe(recipeId).then(value => { if (active) { setRecipe(value); setDocumentText(JSON.stringify(value.document, null, 2)); } }).catch(value => { if (active) setError(value instanceof Error ? value.message : "Unable to load recipe"); });
    return () => { active = false; };
  }, [api, recipeId]);
  const set = (name: string, value: string) => setFields(current => ({...current, [name]: value}));
  const activeRecipeId = recipeId ?? recipe?.recipe_id;

  async function save(event: FormEvent) {
    event.preventDefault(); setError(""); setMessage("");
    try {
      if (recipeId && recipe && api.updateCatalogRecipe) {
        const saved = await api.updateCatalogRecipe(recipeId, recipe.revision_number, JSON.parse(documentText) as CatalogRecipeDocument);
        setRecipe(saved); setDocumentText(JSON.stringify(saved.document, null, 2)); setMessage(`Draft saved as revision ${saved.revision_number}`); return;
      }
      if (!api.uploadSourceBundle) throw new Error("Source bundle upload is unavailable");
      const bundle = await makeSourceBundle({Dockerfile: dockerfile, ...(compose.trim() ? {"compose.yaml": compose} : {})});
      await api.uploadSourceBundle(bundle.sha256, bundle.archive);
      const saved = await api.createCatalogRecipe({slug: fields.slug, document: recipeDocument(fields, bundle.sha256, bundle.archive.length)});
      setRecipe(saved); setDocumentText(JSON.stringify(saved.document, null, 2));
      setMessage(`Source verified and draft saved as revision ${saved.revision_number}`);
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to save recipe"); }
  }
  async function resolve() {
    if (!activeRecipeId || !recipe || !api.resolveCatalogRecipe) return;
    setError("");
    try { const value = await api.resolveCatalogRecipe(activeRecipeId, recipe.revision_number); setRecipe(value); setDocumentText(JSON.stringify(value.document, null, 2)); setConfirmResolve(false); setMessage(`Resolved as sha256:${value.content_sha256}`); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to resolve recipe"); }
  }
  async function attachReport(file: File | undefined) {
    if (!file || !activeRecipeId || !api.attachPublicationReport) return;
    try { await api.attachPublicationReport(activeRecipeId, JSON.parse(await file.text()) as Record<string, unknown>); setMessage("Passing local test report attached to this exact recipe revision."); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to attach test report"); }
  }
  async function downloadExport() {
    if (!activeRecipeId || !recipe || !api.publicationExport) return;
    try { const envelope = await api.publicationExport(activeRecipeId, targetPublisher); const url = URL.createObjectURL(new Blob([JSON.stringify(envelope, null, 2)], {type: "application/json"})); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${targetPublisher}-${recipe.slug}.json`; anchor.click(); URL.revokeObjectURL?.(url); setMessage("Publication JSON downloaded."); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to export recipe"); }
  }

  if (recipeId) return <>
    <div className="page-heading"><div><h2>Recipe details</h2><p>The database stores the typed recipe separately from its content-addressed source bundle and cluster mapping.</p></div></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <form className="recipe-editor" onSubmit={save}><fieldset><legend>Canonical recipe document</legend><label className="wide">Recipe JSON<textarea aria-label="Recipe JSON" rows={30} value={documentText} disabled={recipe?.lifecycle === "resolved"} onChange={event => setDocumentText(event.target.value)}/></label></fieldset><div className="actions">{recipe?.lifecycle !== "resolved" && <><button type="submit">Save draft</button><button type="button" onClick={() => setConfirmResolve(true)}>Resolve recipe</button></>}</div></form>
    {confirmResolve && <section className="confirmation"><h3>Create immutable revision?</h3><p>Resolution locks the recipe metadata and exact source digest. The source still must pass the security gate before a build can be sent.</p><button onClick={() => void resolve()}>Confirm immutable revision</button><button onClick={() => setConfirmResolve(false)}>Cancel</button></section>}
    {recipe?.content_sha256 && <><p className="digest">Canonical content: sha256:{recipe.content_sha256}</p><div className="actions"><a className="button" href={`/catalog/${recipe.recipe_id}/source`}>Security check &amp; build</a><a className="button" href={`/catalog/${recipe.recipe_id}/map`}>Map to cluster</a></div></>}
    {recipe?.lifecycle === "resolved" && <section className="confirmation"><h3>Publish through vonkforge.ai</h3><p>Attach evidence from an actual local build, install, health check, and inference test before export.</p><label>Local test report JSON<input type="file" accept="application/json,.json" onChange={event => void attachReport(event.target.files?.[0])}/></label><label>Target publisher namespace<input value={targetPublisher} onChange={event => setTargetPublisher(event.target.value)}/></label><button type="button" onClick={() => void downloadExport()}>Download publication JSON</button></section>}
  </>;

  const metadataFields: [string, string][] = [["slug", "Recipe slug"], ["title", "Title"], ["description", "Description"], ["modelVersion", "Model-version slug"], ["modelDigest", "Model-version content sha256"], ["executionHarness", "Execution-harness slug"], ["harnessDigest", "Execution-harness content sha256"], ["runtimeDistribution", "Runtime-distribution slug"], ["runtimeDigest", "Runtime-distribution content sha256"], ["repository", "Artifact repository"], ["revision", "Artifact revision"], ["artifactBytes", "Artifact bytes"], ["nodeCount", "Topology node count"], ["memoryBytes", "Runtime memory bytes per node"], ["imageBytes", "Expected image bytes per node"]];
  return <>
    <div className="page-heading"><div><h2>Create local recipe</h2><p>Start from a source template, import WorkloadRun, or describe a fully custom OCI build. No prebuilt image is required.</p></div><a className="button" href="/catalog/import/workload_run">Import WorkloadRun instead</a></div>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <form className="recipe-editor" onSubmit={save}>
      <fieldset><legend>Recipe and capacity</legend>{metadataFields.map(([name, label]) => <label className={name === "description" ? "wide" : undefined} key={name}>{label}{name === "description" ? <textarea value={fields[name]} onChange={event => set(name, event.target.value)} required/> : <input aria-label={label} value={fields[name]} onChange={event => set(name, event.target.value)} required/>}</label>)}<label className="wide">Entrypoint, one argument per line<textarea value={fields.entrypoint} onChange={event => set("entrypoint", event.target.value)} required/></label></fieldset>
      <fieldset><legend>Source bundle</legend><p className="wide">The source is hashed and uploaded to the local catalog first. Compose is inspected as policy metadata and is never granted host privileges. The GPU node independently repeats the check before rootless Podman builds it.</p><label className="wide">Dockerfile<textarea aria-label="Dockerfile" rows={12} value={dockerfile} onChange={event => setDockerfile(event.target.value)} required/></label><label className="wide">Optional Compose policy document<textarea aria-label="Optional Compose policy document" rows={8} value={compose} onChange={event => setCompose(event.target.value)} placeholder="services: {}"/></label></fieldset>
      <div className="actions"><button type="submit">Verify source &amp; save draft</button></div>
    </form>
    {recipe && <section className="confirmation"><h3>Draft created</h3><p>Review the canonical document, resolve it, then run the security gate and select a builder GPU node.</p><a className="button" href={`/catalog/${recipe.recipe_id}`}>Review recipe</a></section>}
  </>;
}

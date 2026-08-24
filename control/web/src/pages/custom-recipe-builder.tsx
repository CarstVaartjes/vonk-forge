import {useEffect, useMemo, useRef, useState} from "react";
import type {CatalogApi, LibraryRecipeDetail} from "../api/types";
import {parseVisualRecipeDocument} from "../lib/library-recipe-document";
import "./library.css";
import "./custom-recipe-builder.css";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;
type BuilderStep = 0 | 1 | 2 | 3 | 4 | 5;
type Preset = "custom" | "vllm" | "diffusers";
type FieldError = {field: string; message: string; step: BuilderStep};

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
const SHA256 = /^[0-9a-f]{64}$/;
const TEMPLATE_DIGESTS = new Set(["0", "1", "2", "3", "4"].map(value => value.repeat(64)));
const steps = [
  {name: "Identity & model", short: "Identity", description: "Name the recipe and bind its exact model."},
  {name: "Runtime", short: "Runtime", description: "Define the build and immutable runtime chain."},
  {name: "Artifacts", short: "Artifacts", description: "List everything that must be downloaded."},
  {name: "Resources & topology", short: "Resources", description: "Set capacity, lifecycle, and exposed endpoints."},
  {name: "Validation & provenance", short: "Evidence", description: "Record checks and where the recipe came from."},
  {name: "Review & create", short: "Review", description: "Check the complete recipe before saving."},
] as const;

const defaultDocument = (): VisualRecipeDocument => ({
  schema_version: 1,
  identity: {publisher: "local", slug: "custom-service"},
  metadata: {title: "Custom model service", description: "A locally maintained model service recipe.", tags: ["custom"]},
  model: {kind: "model-version", publisher: "model-owner", slug: "model-name", content_sha256: "0".repeat(64)},
  execution: {harness: {kind: "execution-harness", publisher: "local", slug: "service", content_sha256: "1".repeat(64)}, patch_bundle: null},
  build: {
    context: {sha256: "2".repeat(64), expected_bytes: 512 * MIB, media_type: "application/octet-stream"},
    dockerfile: "Dockerfile", platform: "linux/arm64", network_mode: "none", network_hosts: [],
    download_bytes: 0, temporary_bytes: 4 * GIB, memory_bytes: 16 * GIB, timeout_seconds: 3600,
  },
  artifacts: [],
  runtime: {
    distribution: {kind: "runtime-distribution", publisher: "local", slug: "runtime", content_sha256: "3".repeat(64)},
    entrypoint: ["run"], lifecycle_pre_start_count: 0, lifecycle_post_stop_count: 0, stop_timeout_seconds: 30,
  },
  interfaces: [],
  validation: {checks: ["health-check"], benchmark_count: 0},
  provenance: {source_kind: "local", source_reference: null, attribution: []},
});

function splitList(value: string): string[] {
  return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
}

function joinList(value: readonly string[]): string {
  return value.join(", ");
}

function formatBytes(value: number): string {
  if (value >= GIB && value % GIB === 0) return `${value / GIB} GiB`;
  if (value >= MIB && value % MIB === 0) return `${value / MIB} MiB`;
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024} KiB`;
  return `${value.toLocaleString()} B`;
}

function errorsFor(document: VisualRecipeDocument, slug: string, selectedStep?: BuilderStep): FieldError[] {
  const errors: FieldError[] = [];
  const add = (step: BuilderStep, field: string, message: string) => {
    if (selectedStep === undefined || selectedStep === step) errors.push({step, field, message});
  };
  const required = (step: BuilderStep, field: string, value: string, label: string) => {
    if (!value.trim()) add(step, field, `Enter ${label.toLowerCase()}.`);
  };
  const digest = (step: BuilderStep, field: string, value: string, label: string) => {
    if (TEMPLATE_DIGESTS.has(value)) add(step, field, `${label} is still a starter placeholder. Replace it with the exact SHA-256.`);
    else if (!SHA256.test(value)) add(step, field, `${label} must be 64 lowercase hexadecimal characters.`);
  };
  const integer = (step: BuilderStep, field: string, value: number, label: string, minimum = 0) => {
    if (!Number.isSafeInteger(value) || value < minimum) add(step, field, `${label} must be at least ${minimum}.`);
  };

  required(0, "recipe-publisher", document.identity.publisher, "a publisher");
  required(0, "recipe-slug", slug, "a recipe slug");
  required(0, "recipe-title", document.metadata.title, "a title");
  required(0, "recipe-description", document.metadata.description, "a description");
  required(0, "model-publisher", document.model.publisher, "a model publisher");
  required(0, "model-slug", document.model.slug, "a model name");
  digest(0, "model-digest", document.model.content_sha256, "Model digest");

  required(1, "harness-publisher", document.execution.harness.publisher, "an execution harness publisher");
  required(1, "harness-slug", document.execution.harness.slug, "an execution harness name");
  digest(1, "harness-digest", document.execution.harness.content_sha256, "Execution harness digest");
  if (document.execution.patch_bundle) {
    required(1, "patch-publisher", document.execution.patch_bundle.publisher, "a patch bundle publisher");
    required(1, "patch-slug", document.execution.patch_bundle.slug, "a patch bundle name");
    digest(1, "patch-digest", document.execution.patch_bundle.content_sha256, "Patch bundle digest");
  }
  required(1, "runtime-publisher", document.runtime.distribution.publisher, "a runtime publisher");
  required(1, "runtime-slug", document.runtime.distribution.slug, "a runtime name");
  digest(1, "runtime-digest", document.runtime.distribution.content_sha256, "Runtime digest");
  if (document.runtime.entrypoint.length === 0) add(1, "runtime-entrypoint", "Add at least one entrypoint argument.");
  required(1, "build-dockerfile", document.build.dockerfile, "a Dockerfile path");
  required(1, "build-platform", document.build.platform, "a target platform");
  required(1, "build-network-mode", document.build.network_mode, "a build network mode");
  digest(1, "context-digest", document.build.context.sha256, "Build context digest");
  required(1, "context-media-type", document.build.context.media_type, "a build context media type");

  document.artifacts.forEach((artifact, index) => {
    required(2, `artifact-${index}-id`, artifact.id, `an ID for artifact ${index + 1}`);
    required(2, `artifact-${index}-kind`, artifact.kind, `a kind for artifact ${index + 1}`);
    required(2, `artifact-${index}-repository`, artifact.repository, `a repository for artifact ${index + 1}`);
    required(2, `artifact-${index}-revision`, artifact.revision, `an immutable revision for artifact ${index + 1}`);
    integer(2, `artifact-${index}-download`, artifact.download_bytes, `Artifact ${index + 1} download size`);
    integer(2, `artifact-${index}-installed`, artifact.installed_bytes, `Artifact ${index + 1} installed size`);
  });

  integer(3, "context-size", document.build.context.expected_bytes, "Build context size");
  integer(3, "download-size", document.build.download_bytes, "Additional download size");
  integer(3, "temporary-size", document.build.temporary_bytes, "Temporary storage");
  integer(3, "memory-size", document.build.memory_bytes, "Build memory");
  integer(3, "build-timeout", document.build.timeout_seconds, "Build timeout", 1);
  integer(3, "pre-start-count", document.runtime.lifecycle_pre_start_count, "Pre-start phase count");
  integer(3, "post-stop-count", document.runtime.lifecycle_post_stop_count, "Post-stop phase count");
  integer(3, "stop-timeout", document.runtime.stop_timeout_seconds, "Stop timeout", 1);
  document.interfaces.forEach((item, index) => {
    required(3, `interface-${index}-adapter`, item.adapter, `an adapter for interface ${index + 1}`);
    if (item.port !== null && item.port !== undefined && (!Number.isSafeInteger(item.port) || item.port < 1 || item.port > 65535)) {
      add(3, `interface-${index}-port`, `Interface ${index + 1} port must be between 1 and 65535.`);
    }
  });

  integer(4, "benchmark-count", document.validation.benchmark_count, "Benchmark count");
  return errors;
}

function TextField({id, label, value, onChange, error, helper, type = "text", min, max}: {
  id: string; label: string; value: string | number; onChange(value: string): void; error?: string; helper?: string;
  type?: "text" | "number" | "url"; min?: number; max?: number;
}) {
  const describedBy = [helper ? `${id}-help` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  return <label htmlFor={id}><span id={`${id}-label`}>{label}</span><input id={id} type={type} min={min} max={max} value={value} onChange={event => onChange(event.target.value)} aria-labelledby={`${id}-label`} aria-invalid={error ? true : undefined} aria-describedby={describedBy}/>{helper && <small id={`${id}-help`} className="builder-field-help">{helper}</small>}{error && <span id={`${id}-error`} className="builder-field-error">{error}</span>}</label>;
}

function DigestField(props: Omit<Parameters<typeof TextField>[0], "helper">) {
  return <TextField {...props} helper="SHA-256 without the sha256: prefix. The repeated-digit starter value cannot be saved."/>;
}

const byteUnits = [
  {label: "B", factor: 1}, {label: "KiB", factor: 1024}, {label: "MiB", factor: MIB}, {label: "GiB", factor: GIB}, {label: "TiB", factor: 1024 ** 4},
] as const;

function preferredUnit(value: number): number {
  for (let index = byteUnits.length - 1; index > 0; index -= 1) if (value >= byteUnits[index].factor && value % byteUnits[index].factor === 0) return index;
  return 0;
}

function HumanBytesField({id, label, value, onChange, error}: {id: string; label: string; value: number; onChange(value: number): void; error?: string}) {
  const [unitIndex, setUnitIndex] = useState(() => preferredUnit(value));
  const unit = byteUnits[unitIndex];
  return <fieldset className="builder-unit-field" aria-describedby={error ? `${id}-error` : undefined}>
    <legend>{label}</legend>
    <div><input id={id} aria-label={`${label} amount`} type="number" min="0" step="any" value={value / unit.factor} aria-invalid={error ? true : undefined} onChange={event => onChange(Math.round(Number(event.target.value) * unit.factor))}/><select aria-label={`${label} unit`} value={unitIndex} onChange={event => setUnitIndex(Number(event.target.value))}>{byteUnits.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></div>
    {error && <span id={`${id}-error`} className="builder-field-error">{error}</span>}
  </fieldset>;
}

function ErrorSummary({errors, onSelect}: {errors: FieldError[]; onSelect(error: FieldError): void}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => ref.current?.focus(), [errors]);
  if (errors.length === 0) return null;
  return <div className="builder-error-summary" role="alert" tabIndex={-1} ref={ref}>
    <h3>Check {errors.length === 1 ? "this answer" : `${errors.length} answers`}</h3>
    <ul>{errors.map(error => <li key={`${error.field}-${error.message}`}><a href={`#${error.field}`} onClick={event => { event.preventDefault(); onSelect(error); queueMicrotask(() => globalThis.document.getElementById(error.field)?.focus()); }}>{error.message}</a></li>)}</ul>
  </div>;
}

function ReviewRow({term, children}: {term: string; children: React.ReactNode}) {
  return <div><dt>{term}</dt><dd>{children}</dd></div>;
}

export function CustomRecipeBuilderPage({api, onNavigate, onBusyChange}: {
  api: CatalogApi;
  onNavigate(nextUrl: string): void;
  onBusyChange?(busy: boolean): void;
}) {
  const initial = useMemo(defaultDocument, []);
  const [document, setDocument] = useState(initial);
  const [slug, setSlug] = useState(initial.identity.slug);
  const [documentText, setDocumentText] = useState(() => JSON.stringify(initial, null, 2));
  const [step, setStep] = useState<BuilderStep>(0);
  const [highestStep, setHighestStep] = useState<BuilderStep>(0);
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [jsonError, setJsonError] = useState("");
  const [preset, setPreset] = useState<Preset>("custom");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [createdRecipeId, setCreatedRecipeId] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  function update(updater: (current: VisualRecipeDocument) => VisualRecipeDocument) {
    setDocument(current => {
      const next = updater(current);
      setDocumentText(JSON.stringify(next, null, 2));
      return next;
    });
    setErrors([]);
    setJsonError("");
    setStatus("");
  }

  function changeSlug(value: string) {
    setSlug(value);
    update(current => ({...current, identity: {...current.identity, slug: value}}));
  }

  function applyPreset() {
    update(current => {
      if (preset === "custom") return defaultDocument();
      if (preset === "vllm") return {
        ...current,
        identity: {...current.identity, slug: "custom-vllm-chat"},
        metadata: {title: "vLLM chat service", description: "An OpenAI-compatible text generation service powered by vLLM.", tags: ["chat", "reasoning", "vllm"]},
        execution: {...current.execution, harness: {...current.execution.harness, slug: "vllm-openai"}},
        build: {...current.build, download_bytes: 80 * GIB, temporary_bytes: 16 * GIB, memory_bytes: 64 * GIB},
        artifacts: [{id: "model-weights", kind: "model", repository: "owner/model-name", revision: "immutable-revision", download_bytes: 80 * GIB, installed_bytes: 80 * GIB, roles: ["leader", "worker"]}],
        runtime: {...current.runtime, distribution: {...current.runtime.distribution, slug: "vllm-cuda"}, entrypoint: ["python", "-m", "vllm.entrypoints.openai.api_server"]},
        interfaces: [{adapter: "openai", port: 8000, model_aliases: ["model-name"], health_path: "/health", path: "/v1"}],
        validation: {checks: ["health-check", "chat-completion-smoke-test"], benchmark_count: 0},
      };
      return {
        ...current,
        identity: {...current.identity, slug: "custom-diffusers-image"},
        metadata: {title: "Diffusers image service", description: "An HTTP image-generation service powered by Diffusers.", tags: ["image-generation", "diffusers"]},
        execution: {...current.execution, harness: {...current.execution.harness, slug: "diffusers-service"}},
        build: {...current.build, download_bytes: 32 * GIB, temporary_bytes: 12 * GIB, memory_bytes: 48 * GIB},
        artifacts: [{id: "model-weights", kind: "model", repository: "owner/model-name", revision: "immutable-revision", download_bytes: 32 * GIB, installed_bytes: 32 * GIB, roles: ["leader"]}],
        runtime: {...current.runtime, distribution: {...current.runtime.distribution, slug: "diffusers-cuda"}, entrypoint: ["python", "-m", "service"]},
        interfaces: [{adapter: "http", port: 8000, model_aliases: [], health_path: "/health", path: "/generate"}],
        validation: {checks: ["health-check", "image-generation-smoke-test"], benchmark_count: 0},
      };
    });
    const nextSlug = preset === "custom" ? "custom-service" : preset === "vllm" ? "custom-vllm-chat" : "custom-diffusers-image";
    setSlug(nextSlug);
  }

  function goNext() {
    const nextErrors = errorsFor(document, slug, step);
    setErrors(nextErrors);
    if (nextErrors.length > 0) return;
    const nextStep = Math.min(5, step + 1) as BuilderStep;
    setStep(nextStep);
    setHighestStep(current => Math.max(current, nextStep) as BuilderStep);
    queueMicrotask(() => heading.current?.focus());
  }

  function goTo(nextStep: BuilderStep) {
    if (nextStep > highestStep) return;
    setStep(nextStep);
    setErrors([]);
    queueMicrotask(() => heading.current?.focus());
  }

  async function createRecipe() {
    const allErrors = errorsFor(document, slug);
    const parsed = parseVisualRecipeDocument(documentText);
    if (!parsed.ok) allErrors.push({field: "advanced-json", message: parsed.error, step: 5});
    setErrors(allErrors);
    if (allErrors.length > 0 || !parsed.ok) return;
    setBusy(true);
    onBusyChange?.(true);
    setStatus("");
    try {
      const created = await api.createCatalogRecipe({slug, document: parsed.document});
      setCreatedRecipeId(created.recipe_id);
      setStatus("Recipe saved");
    } catch (value) {
      setStatus(value instanceof Error ? value.message.slice(0, 256) : "Unable to save recipe");
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  }

  const error = (field: string) => errors.find(item => item.field === field)?.message;
  const updateArtifact = (index: number, changes: Partial<VisualRecipeDocument["artifacts"][number]>) => update(current => ({...current, artifacts: current.artifacts.map((item, itemIndex) => itemIndex === index ? {...item, ...changes} : item)}));
  const updateInterface = (index: number, changes: Partial<VisualRecipeDocument["interfaces"][number]>) => update(current => ({...current, interfaces: current.interfaces.map((item, itemIndex) => itemIndex === index ? {...item, ...changes} : item)}));

  return <div className="recipe-builder-page">
    <header className="builder-hero">
      <div><p className="fleet-kicker">Local recipe builder</p><h2 ref={heading} tabIndex={-1}>Create custom recipe</h2><p>Build a reviewable recipe one decision at a time. Nothing is saved until the final step.</p></div>
      <button type="button" className="button secondary" disabled={busy} onClick={() => onNavigate("/library")}>Back to Library</button>
    </header>

    <nav className="builder-progress" aria-label="Recipe builder progress">
      <ol>{steps.map((item, index) => <li key={item.name} className={index === step ? "current" : index < step ? "complete" : ""}>
        <button type="button" disabled={index > highestStep || busy} aria-current={index === step ? "step" : undefined} onClick={() => goTo(index as BuilderStep)}><span>{index + 1}</span><strong>{item.short}</strong></button>
      </li>)}</ol>
    </nav>

    <section className="builder-workspace" aria-labelledby="builder-step-heading">
      <header><div><span>Step {step + 1} of {steps.length}</span><h3 id="builder-step-heading">{steps[step].name}</h3><p>{steps[step].description}</p></div><span className="library-panel-badge">Draft</span></header>
      <ErrorSummary errors={errors} onSelect={selected => { setStep(selected.step); setHighestStep(current => Math.max(current, selected.step) as BuilderStep); }}/>
      <fieldset className="builder-form-lock" disabled={busy}>

      {step === 0 && <div className="builder-step-fields">
        <fieldset className="builder-preset"><legend>Start from a useful shape</legend><p>Presets fill common runtime, artifact, resource, and endpoint values. Exact component digests remain yours to verify.</p><div><select aria-label="Recipe starting point" value={preset} onChange={event => setPreset(event.target.value as Preset)}><option value="custom">Custom model service</option><option value="vllm">vLLM chat service</option><option value="diffusers">Diffusers image service</option></select><button type="button" className="button secondary" onClick={applyPreset}>Apply starting point</button></div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Recipe identity</legend><div className="custom-recipe-fields custom-recipe-fields-two">
          <TextField id="recipe-publisher" label="Publisher" value={document.identity.publisher} onChange={value => update(current => ({...current, identity: {...current.identity, publisher: value}}))} error={error("recipe-publisher")}/>
          <TextField id="recipe-slug" label="Recipe slug" value={slug} onChange={changeSlug} error={error("recipe-slug")} helper="Stable URL-safe name used by the local catalog."/>
          <TextField id="recipe-title" label="Display name" value={document.metadata.title} onChange={value => update(current => ({...current, metadata: {...current.metadata, title: value}}))} error={error("recipe-title")}/>
          <TextField id="recipe-tags" label="Capabilities and tags" value={joinList(document.metadata.tags)} onChange={value => update(current => ({...current, metadata: {...current.metadata, tags: splitList(value)}}))} helper="Comma-separated, for example chat, vision, reasoning."/>
          <label className="custom-recipe-wide" htmlFor="recipe-description">Description<textarea id="recipe-description" rows={3} value={document.metadata.description} aria-invalid={error("recipe-description") ? true : undefined} aria-describedby={error("recipe-description") ? "recipe-description-error" : undefined} onChange={event => update(current => ({...current, metadata: {...current.metadata, description: event.target.value}}))}/>{error("recipe-description") && <span id="recipe-description-error" className="builder-field-error">{error("recipe-description")}</span>}</label>
        </div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Exact model version</legend><p className="custom-recipe-section-help">Use a friendly publisher and model name here; the digest keeps the version immutable.</p><div className="custom-recipe-fields custom-recipe-fields-two">
          <TextField id="model-publisher" label="Model creator or publisher" value={document.model.publisher} onChange={value => update(current => ({...current, model: {...current.model, publisher: value}}))} error={error("model-publisher")}/>
          <TextField id="model-slug" label="Model name" value={document.model.slug} onChange={value => update(current => ({...current, model: {...current.model, slug: value}}))} error={error("model-slug")}/>
          <div className="custom-recipe-wide"><DigestField id="model-digest" label="Exact model digest" value={document.model.content_sha256} onChange={value => update(current => ({...current, model: {...current.model, content_sha256: value}}))} error={error("model-digest")}/></div>
        </div></fieldset>
      </div>}

      {step === 1 && <div className="builder-step-fields">
        <div className="custom-recipe-card-grid">
          <fieldset className="custom-recipe-section"><legend>Execution harness</legend><div className="custom-recipe-fields">
            <TextField id="harness-publisher" label="Publisher" value={document.execution.harness.publisher} onChange={value => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, publisher: value}}}))} error={error("harness-publisher")}/>
            <TextField id="harness-slug" label="Harness name" value={document.execution.harness.slug} onChange={value => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, slug: value}}}))} error={error("harness-slug")}/>
            <div className="custom-recipe-wide"><DigestField id="harness-digest" label="Exact harness digest" value={document.execution.harness.content_sha256} onChange={value => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, content_sha256: value}}}))} error={error("harness-digest")}/></div>
          </div></fieldset>
          <fieldset className="custom-recipe-section"><legend>Runtime distribution</legend><div className="custom-recipe-fields">
            <TextField id="runtime-publisher" label="Publisher" value={document.runtime.distribution.publisher} onChange={value => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, publisher: value}}}))} error={error("runtime-publisher")}/>
            <TextField id="runtime-slug" label="Runtime name" value={document.runtime.distribution.slug} onChange={value => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, slug: value}}}))} error={error("runtime-slug")}/>
            <div className="custom-recipe-wide"><DigestField id="runtime-digest" label="Exact runtime digest" value={document.runtime.distribution.content_sha256} onChange={value => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, content_sha256: value}}}))} error={error("runtime-digest")}/></div>
          </div></fieldset>
        </div>
        <fieldset className="custom-recipe-section"><legend>Process command</legend><div className="custom-recipe-fields custom-recipe-fields-two"><TextField id="runtime-entrypoint" label="Entrypoint arguments" value={joinList(document.runtime.entrypoint)} onChange={value => update(current => ({...current, runtime: {...current.runtime, entrypoint: splitList(value)}}))} error={error("runtime-entrypoint")} helper="Comma-separated arguments, in execution order."/></div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Build contract</legend><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="build-dockerfile" label="Dockerfile path" value={document.build.dockerfile} onChange={value => update(current => ({...current, build: {...current.build, dockerfile: value}}))} error={error("build-dockerfile")}/>
          <TextField id="build-platform" label="Target platform" value={document.build.platform} onChange={value => update(current => ({...current, build: {...current.build, platform: value}}))} error={error("build-platform")}/>
          <TextField id="build-network-mode" label="Build network mode" value={document.build.network_mode} onChange={value => update(current => ({...current, build: {...current.build, network_mode: value}}))} error={error("build-network-mode")}/>
          <TextField id="context-media-type" label="Build context media type" value={document.build.context.media_type} onChange={value => update(current => ({...current, build: {...current.build, context: {...current.build.context, media_type: value}}}))} error={error("context-media-type")}/>
          <TextField id="network-hosts" label="Allowed network hosts" value={joinList(document.build.network_hosts)} onChange={value => update(current => ({...current, build: {...current.build, network_hosts: splitList(value)}}))} helper="Comma-separated. Leave empty for no allowlisted hosts."/>
          <div className="custom-recipe-wide"><DigestField id="context-digest" label="Exact build context digest" value={document.build.context.sha256} onChange={value => update(current => ({...current, build: {...current.build, context: {...current.build.context, sha256: value}}}))} error={error("context-digest")}/></div>
        </div></fieldset>
        <label className="custom-recipe-checkbox"><input type="checkbox" checked={document.execution.patch_bundle !== null} onChange={event => update(current => ({...current, execution: {...current.execution, patch_bundle: event.target.checked ? {kind: "patch-bundle", publisher: "local", slug: "patch", content_sha256: "4".repeat(64)} : null}}))}/> Add an immutable patch bundle</label>
        {document.execution.patch_bundle && <fieldset className="custom-recipe-section"><legend>Patch bundle</legend><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="patch-publisher" label="Publisher" value={document.execution.patch_bundle.publisher} onChange={value => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, publisher: value} : null}}))} error={error("patch-publisher")}/>
          <TextField id="patch-slug" label="Patch name" value={document.execution.patch_bundle.slug} onChange={value => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, slug: value} : null}}))} error={error("patch-slug")}/>
          <DigestField id="patch-digest" label="Exact patch digest" value={document.execution.patch_bundle.content_sha256} onChange={value => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, content_sha256: value} : null}}))} error={error("patch-digest")}/>
        </div></fieldset>}
      </div>}

      {step === 2 && <div className="builder-step-fields">
        <div className="builder-step-callout"><div><strong>{document.artifacts.length}</strong><span>{document.artifacts.length === 1 ? "artifact" : "artifacts"}</span></div><p>Artifacts are optional. Add every immutable model, tokenizer, adapter, or supporting bundle the recipe downloads.</p></div>
        <div className="custom-recipe-repeat-list">
          {document.artifacts.map((artifact, index) => <fieldset className="custom-recipe-section" key={`${artifact.id}-${index}`}><legend>Artifact {index + 1}</legend><div className="custom-recipe-card-heading"><p className="custom-recipe-section-help">Use a readable name and an immutable repository revision.</p><button type="button" className="button secondary" onClick={() => update(current => ({...current, artifacts: current.artifacts.filter((_, itemIndex) => itemIndex !== index)}))}>Remove artifact {index + 1}</button></div><div className="custom-recipe-fields custom-recipe-fields-two">
            <TextField id={`artifact-${index}-id`} label="Artifact name" value={artifact.id} onChange={value => updateArtifact(index, {id: value})} error={error(`artifact-${index}-id`)}/>
            <TextField id={`artifact-${index}-kind`} label="Kind" value={artifact.kind} onChange={value => updateArtifact(index, {kind: value})} error={error(`artifact-${index}-kind`)} helper="For example model, tokenizer, or adapter."/>
            <TextField id={`artifact-${index}-repository`} label="Original repository" value={artifact.repository} onChange={value => updateArtifact(index, {repository: value})} error={error(`artifact-${index}-repository`)}/>
            <TextField id={`artifact-${index}-revision`} label="Immutable revision" value={artifact.revision} onChange={value => updateArtifact(index, {revision: value})} error={error(`artifact-${index}-revision`)}/>
            <HumanBytesField id={`artifact-${index}-download`} label="Download size" value={artifact.download_bytes} onChange={value => updateArtifact(index, {download_bytes: value})} error={error(`artifact-${index}-download`)}/>
            <HumanBytesField id={`artifact-${index}-installed`} label="Installed size" value={artifact.installed_bytes} onChange={value => updateArtifact(index, {installed_bytes: value})} error={error(`artifact-${index}-installed`)}/>
            <TextField id={`artifact-${index}-roles`} label="Node roles" value={joinList(artifact.roles)} onChange={value => updateArtifact(index, {roles: splitList(value)})} helper="Comma-separated, for example leader, worker."/>
          </div></fieldset>)}
          <button type="button" className="button secondary custom-recipe-add" onClick={() => update(current => ({...current, artifacts: [...current.artifacts, {id: `artifact-${current.artifacts.length + 1}`, kind: "model", repository: "", revision: "", download_bytes: 0, installed_bytes: 0, roles: []}]}))}>Add artifact</button>
        </div>
      </div>}

      {step === 3 && <div className="builder-step-fields">
        <div className="builder-resource-summary" aria-label="Resource envelope"><div><span>Context</span><strong>{formatBytes(document.build.context.expected_bytes)}</strong></div><div><span>Download</span><strong>{formatBytes(document.build.download_bytes + document.artifacts.reduce((total, item) => total + item.download_bytes, 0))}</strong></div><div><span>Temporary</span><strong>{formatBytes(document.build.temporary_bytes)}</strong></div><div><span>Build memory</span><strong>{formatBytes(document.build.memory_bytes)}</strong></div></div>
        <fieldset className="custom-recipe-section"><legend>Resource envelope</legend><p className="custom-recipe-section-help">Enter normal human units; the recipe is saved using exact bytes.</p><div className="builder-unit-grid">
          <HumanBytesField id="context-size" label="Build context" value={document.build.context.expected_bytes} onChange={value => update(current => ({...current, build: {...current.build, context: {...current.build.context, expected_bytes: value}}}))} error={error("context-size")}/>
          <HumanBytesField id="download-size" label="Additional downloads" value={document.build.download_bytes} onChange={value => update(current => ({...current, build: {...current.build, download_bytes: value}}))} error={error("download-size")}/>
          <HumanBytesField id="temporary-size" label="Temporary storage" value={document.build.temporary_bytes} onChange={value => update(current => ({...current, build: {...current.build, temporary_bytes: value}}))} error={error("temporary-size")}/>
          <HumanBytesField id="memory-size" label="Build memory" value={document.build.memory_bytes} onChange={value => update(current => ({...current, build: {...current.build, memory_bytes: value}}))} error={error("memory-size")}/>
        </div><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="build-timeout" label="Build timeout (seconds)" type="number" min={1} value={document.build.timeout_seconds} onChange={value => update(current => ({...current, build: {...current.build, timeout_seconds: Number(value)}}))} error={error("build-timeout")}/>
          <TextField id="pre-start-count" label="Pre-start phases" type="number" min={0} value={document.runtime.lifecycle_pre_start_count} onChange={value => update(current => ({...current, runtime: {...current.runtime, lifecycle_pre_start_count: Number(value)}}))} error={error("pre-start-count")}/>
          <TextField id="post-stop-count" label="Post-stop phases" type="number" min={0} value={document.runtime.lifecycle_post_stop_count} onChange={value => update(current => ({...current, runtime: {...current.runtime, lifecycle_post_stop_count: Number(value)}}))} error={error("post-stop-count")}/>
          <TextField id="stop-timeout" label="Stop timeout (seconds)" type="number" min={1} value={document.runtime.stop_timeout_seconds} onChange={value => update(current => ({...current, runtime: {...current.runtime, stop_timeout_seconds: Number(value)}}))} error={error("stop-timeout")}/>
        </div></fieldset>
        <div className="builder-topology-preview" aria-label="Service topology preview"><div><span>Model</span><strong>{document.model.publisher}/{document.model.slug}</strong></div><span aria-hidden="true">→</span><div><span>Runtime</span><strong>{document.runtime.distribution.slug}</strong></div><span aria-hidden="true">→</span><div><span>Endpoints</span><strong>{document.interfaces.length || "None"}</strong></div></div>
        <fieldset className="custom-recipe-section"><legend>Exposed interfaces</legend><p className="custom-recipe-section-help">These endpoints describe how operators and clients reach the running topology.</p><div className="custom-recipe-repeat-list">
          {document.interfaces.map((item, index) => <div className="custom-recipe-card" key={`${item.adapter}-${index}`}><div className="custom-recipe-card-heading"><h4>Interface {index + 1}</h4><button type="button" className="button secondary" onClick={() => update(current => ({...current, interfaces: current.interfaces.filter((_, itemIndex) => itemIndex !== index)}))}>Remove interface {index + 1}</button></div><div className="custom-recipe-fields custom-recipe-fields-three">
            <TextField id={`interface-${index}-adapter`} label="Adapter" value={item.adapter} onChange={value => updateInterface(index, {adapter: value})} error={error(`interface-${index}-adapter`)}/>
            <TextField id={`interface-${index}-port`} label="Port" type="number" min={1} max={65535} value={item.port ?? ""} onChange={value => updateInterface(index, {port: value ? Number(value) : null})} error={error(`interface-${index}-port`)}/>
            <TextField id={`interface-${index}-health`} label="Health path" value={item.health_path ?? ""} onChange={value => updateInterface(index, {health_path: value || null})}/>
            <TextField id={`interface-${index}-path`} label="Job or API path" value={item.path ?? ""} onChange={value => updateInterface(index, {path: value || null})}/>
            <TextField id={`interface-${index}-aliases`} label="Model aliases" value={joinList(item.model_aliases ?? [])} onChange={value => updateInterface(index, {model_aliases: splitList(value)})}/>
          </div></div>)}
          <button type="button" className="button secondary custom-recipe-add" onClick={() => update(current => ({...current, interfaces: [...current.interfaces, {adapter: "http", port: 8000, model_aliases: [], health_path: "/health", path: "/v1"}]}))}>Add interface</button>
        </div></fieldset>
      </div>}

      {step === 4 && <div className="builder-step-fields">
        <fieldset className="custom-recipe-section"><legend>Validation evidence</legend><p className="custom-recipe-section-help">Name the checks this recipe has passed. Counts are evidence, not approval.</p><div className="custom-recipe-fields custom-recipe-fields-two">
          <TextField id="validation-checks" label="Validation checks" value={joinList(document.validation.checks)} onChange={value => update(current => ({...current, validation: {...current.validation, checks: splitList(value)}}))} helper="Comma-separated, for example health-check, smoke-test."/>
          <TextField id="benchmark-count" label="Recorded benchmarks" type="number" min={0} value={document.validation.benchmark_count} onChange={value => update(current => ({...current, validation: {...current.validation, benchmark_count: Number(value)}}))} error={error("benchmark-count")}/>
        </div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Provenance</legend><p className="custom-recipe-section-help">Record the origin in operator-friendly terms; immutable technical identities stay available in the review.</p><div className="custom-recipe-fields custom-recipe-fields-two">
          <label htmlFor="source-kind">Source kind<select id="source-kind" value={document.provenance.source_kind} onChange={event => update(current => ({...current, provenance: {...current.provenance, source_kind: event.target.value as VisualRecipeDocument["provenance"]["source_kind"]}}))}><option value="local">Created locally</option><option value="workload_run">Derived from a workload run</option><option value="global">Imported from a public source</option><option value="fork">Forked from another recipe</option></select></label>
          <TextField id="source-reference" label="Original source or repository" value={document.provenance.source_reference ?? ""} onChange={value => update(current => ({...current, provenance: {...current.provenance, source_reference: value || null}}))} helper="URL, recipe reference, or workload run name."/>
          <div className="custom-recipe-wide"><TextField id="source-attribution" label="Creator and attribution" value={joinList(document.provenance.attribution)} onChange={value => update(current => ({...current, provenance: {...current.provenance, attribution: splitList(value)}}))} helper="Comma-separated names or organizations."/></div>
        </div></fieldset>
      </div>}

      {step === 5 && <div className="builder-review">
        <section><header><div><span>1</span><h4>Identity & model</h4></div><button type="button" className="button secondary" aria-label="Change identity and model" onClick={() => goTo(0)}>Change</button></header><dl><ReviewRow term="Recipe">{document.metadata.title}</ReviewRow><ReviewRow term="Catalog name">{document.identity.publisher}/{slug}</ReviewRow><ReviewRow term="Model">{document.model.publisher}/{document.model.slug}</ReviewRow><ReviewRow term="Description">{document.metadata.description}</ReviewRow></dl></section>
        <section><header><div><span>2</span><h4>Runtime</h4></div><button type="button" className="button secondary" aria-label="Change runtime" onClick={() => goTo(1)}>Change</button></header><dl><ReviewRow term="Execution harness">{document.execution.harness.publisher}/{document.execution.harness.slug}</ReviewRow><ReviewRow term="Runtime">{document.runtime.distribution.publisher}/{document.runtime.distribution.slug}</ReviewRow><ReviewRow term="Entrypoint"><code>{document.runtime.entrypoint.join(" ")}</code></ReviewRow><ReviewRow term="Build">{document.build.platform} · {document.build.dockerfile} · network {document.build.network_mode}</ReviewRow></dl></section>
        <section><header><div><span>3</span><h4>Artifacts</h4></div><button type="button" className="button secondary" aria-label="Change artifacts" onClick={() => goTo(2)}>Change</button></header><dl><ReviewRow term="Artifacts">{document.artifacts.length}</ReviewRow><ReviewRow term="Total download">{formatBytes(document.artifacts.reduce((total, item) => total + item.download_bytes, 0))}</ReviewRow><ReviewRow term="Repositories">{document.artifacts.length ? document.artifacts.map(item => item.repository).join(", ") : "None"}</ReviewRow></dl></section>
        <section><header><div><span>4</span><h4>Resources & topology</h4></div><button type="button" className="button secondary" aria-label="Change resources and topology" onClick={() => goTo(3)}>Change</button></header><dl><ReviewRow term="Build memory">{formatBytes(document.build.memory_bytes)}</ReviewRow><ReviewRow term="Temporary storage">{formatBytes(document.build.temporary_bytes)}</ReviewRow><ReviewRow term="Endpoints">{document.interfaces.length ? document.interfaces.map(item => `${item.adapter}${item.port ? ` :${item.port}` : ""}`).join(", ") : "None"}</ReviewRow><ReviewRow term="Lifecycle">{document.runtime.lifecycle_pre_start_count} pre-start · {document.runtime.lifecycle_post_stop_count} post-stop</ReviewRow></dl></section>
        <section><header><div><span>5</span><h4>Validation & provenance</h4></div><button type="button" className="button secondary" aria-label="Change validation and provenance" onClick={() => goTo(4)}>Change</button></header><dl><ReviewRow term="Checks">{document.validation.checks.join(", ") || "None recorded"}</ReviewRow><ReviewRow term="Benchmarks">{document.validation.benchmark_count}</ReviewRow><ReviewRow term="Origin">{document.provenance.source_kind.replaceAll("_", " ")}</ReviewRow><ReviewRow term="Attribution">{document.provenance.attribution.join(", ") || "None recorded"}</ReviewRow></dl></section>
        <details className="builder-technical-review"><summary>Technical identities and digests</summary><dl><ReviewRow term="Model digest"><code>{document.model.content_sha256}</code></ReviewRow><ReviewRow term="Harness digest"><code>{document.execution.harness.content_sha256}</code></ReviewRow><ReviewRow term="Runtime digest"><code>{document.runtime.distribution.content_sha256}</code></ReviewRow><ReviewRow term="Build context digest"><code>{document.build.context.sha256}</code></ReviewRow></dl></details>
      </div>}

      <details className="library-json-fallback"><summary>Advanced JSON</summary><div className="library-json-fallback-content"><p>Edit or paste the complete canonical document. Valid JSON immediately updates every step; invalid JSON is kept here for correction.</p><label htmlFor="advanced-json">Recipe document<textarea id="advanced-json" aria-label="Recipe document" rows={12} spellCheck={false} value={documentText} aria-invalid={jsonError ? true : undefined} aria-describedby={jsonError ? "advanced-json-error" : undefined} onChange={event => { const value = event.target.value; setDocumentText(value); const parsed = parseVisualRecipeDocument(value); if (parsed.ok) { setDocument(parsed.document); setSlug(parsed.document.identity.slug); setJsonError(""); setErrors([]); } else setJsonError(parsed.error); }}/>{jsonError && <span id="advanced-json-error" className="builder-field-error">{jsonError}</span>}</label></div></details>
      </fieldset>

      <footer className="builder-actions">
        <button type="button" className="button secondary" disabled={step === 0 || busy} onClick={() => goTo((step - 1) as BuilderStep)}>Previous</button>
        <span aria-live="polite">{status && <strong role={createdRecipeId ? "status" : "alert"} className={createdRecipeId ? "builder-success" : "builder-save-error"}>{status}</strong>}</span>
        <div>{createdRecipeId && <button type="button" className="button secondary" onClick={() => onNavigate(`/library/recipes/${encodeURIComponent(createdRecipeId)}`)}>View saved recipe</button>}{step < 5 ? <button type="button" className="button" onClick={goNext}>Continue</button> : <button type="button" className="button" disabled={busy || Boolean(createdRecipeId)} onClick={() => void createRecipe()}>{busy ? "Creating recipe…" : "Create recipe"}</button>}</div>
      </footer>
    </section>
  </div>;
}

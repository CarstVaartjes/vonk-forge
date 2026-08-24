import {useEffect, useMemo, useRef, useState} from "react";
import type {CatalogApi} from "../api/types";
import {
  createCanonicalRecipeDocument,
  parseCanonicalRecipeDocument,
} from "../lib/canonical-recipe-document";
import type {
  CanonicalArtifact,
  CanonicalInterface,
  CanonicalRecipeDocument,
  InterfaceAdapter,
  PresetName,
  TopologyMode,
} from "../lib/canonical-recipe-document";
import "./library.css";
import "./custom-recipe-builder.css";

type BuilderStep = 0 | 1 | 2 | 3 | 4 | 5;
type FieldError = {field: string; message: string; step: BuilderStep};
type BuilderDraft = {document: CanonicalRecipeDocument; documentText: string; highestStep: BuilderStep; slug: string; step: BuilderStep};

export const CUSTOM_RECIPE_DRAFT_STORAGE_KEY = "vonk-forge:custom-recipe-draft:v1";

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
const SHA256 = /^[0-9a-f]{64}$/;
const SLUG = /^[a-z0-9][a-z0-9-]{1,62}$/;
const NAME = /^[a-z][a-z0-9_-]{0,63}$/;
const TEMPLATE_DIGESTS = new Set(["0", "1", "2", "3", "4"].map(value => value.repeat(64)));
const steps = [
  {name: "Identity & model", short: "Identity", description: "Name the recipe and bind its exact model."},
  {name: "Runtime", short: "Runtime", description: "Define the build and immutable runtime chain."},
  {name: "Artifacts", short: "Artifacts", description: "List everything that must be downloaded."},
  {name: "Resources & topology", short: "Resources", description: "Set capacity, lifecycle, and exposed endpoints."},
  {name: "Validation & provenance", short: "Evidence", description: "Record checks and where the recipe came from."},
  {name: "Review & create", short: "Review", description: "Check the complete recipe before saving."},
] as const;

const defaultDocument = () => createCanonicalRecipeDocument("custom");

function storedBuilderDraft(): BuilderDraft | undefined {
  try {
    const raw = sessionStorage.getItem(CUSTOM_RECIPE_DRAFT_STORAGE_KEY);
    if (!raw || raw.length > 1_000_000) return undefined;
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (value.version !== 1 || typeof value.document !== "object" || value.document === null) return undefined;
    const parsed = parseCanonicalRecipeDocument(JSON.stringify(value.document));
    if (!parsed.ok) return undefined;
    const step = typeof value.step === "number" && Number.isInteger(value.step) && value.step >= 0 && value.step <= 5 ? value.step as BuilderStep : 0;
    const highestStep = typeof value.highestStep === "number" && Number.isInteger(value.highestStep) && value.highestStep >= step && value.highestStep <= 5 ? value.highestStep as BuilderStep : step;
    return {
      document: parsed.document,
      documentText: typeof value.documentText === "string" && value.documentText.length <= 900_000 ? value.documentText : JSON.stringify(parsed.document, null, 2),
      highestStep,
      slug: typeof value.slug === "string" ? value.slug.slice(0, 63) : parsed.document.identity.slug,
      step,
    };
  } catch {
    return undefined;
  }
}

export function discardStoredCustomRecipeDraft() {
  try { sessionStorage.removeItem(CUSTOM_RECIPE_DRAFT_STORAGE_KEY); } catch { /* Draft recovery is optional when storage is unavailable. */ }
}

function splitList(value: string): string[] {
  return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean);
}

function joinList(value: readonly string[]): string {
  return value.join(", ");
}

function resizeCommands(commands: string[][], count: number, prefix: string): string[][] {
  const size = Math.max(0, Math.trunc(count));
  return commands.slice(0, size).concat(Array.from({length: Math.max(0, size - commands.length)}, (_, index) => [prefix, String(commands.length + index + 1)]));
}

function interfaceDefaults(adapter: InterfaceAdapter): CanonicalInterface {
  return adapter === "openai"
    ? {adapter, port: 8000, model_aliases: ["model-name"], health_path: "/health"}
    : {adapter, path: "/jobs"};
}

function topologyWithNodeCount(document: CanonicalRecipeDocument, count: number): CanonicalRecipeDocument {
  const nodeCount = Math.max(1, Math.trunc(count));
  const template = document.topology.roles[0] ?? createCanonicalRecipeDocument().topology.roles[0];
  const artifactIds = document.artifacts.map(artifact => artifact.id);
  const roles = nodeCount === 1
    ? [{...structuredClone(template), name: "entrypoint", count: 1, endpoint_owner: true, artifacts: artifactIds}]
    : [
        {...structuredClone(template), name: "leader", count: 1, endpoint_owner: true, artifacts: artifactIds},
        {...structuredClone(template), name: "worker", count: nodeCount - 1, endpoint_owner: false, artifacts: artifactIds},
      ];
  const roleNames = roles.map(role => role.name);
  return {
    ...document,
    artifacts: document.artifacts.map(artifact => ({...artifact, roles: roleNames})),
    topology: {
      ...document.topology,
      name: nodeCount === 1 ? "solo" : `${nodeCount}-spark`,
      mode: nodeCount === 1 ? "single" : "tensor_parallel",
      node_count: nodeCount,
      roles,
      parallelism: {...document.topology.parallelism, world_size: nodeCount, tensor: nodeCount, pipeline: 1, data: 1},
      fabric: {...document.topology.fabric, connectivity: nodeCount === 1 ? "none" : "connected"},
      start_order: roleNames,
      stop_order: [...roleNames].reverse(),
    },
  };
}

function formatBytes(value: number): string {
  if (value >= GIB && value % GIB === 0) return `${value / GIB} GiB`;
  if (value >= MIB && value % MIB === 0) return `${value / MIB} MiB`;
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024} KiB`;
  return `${value.toLocaleString()} B`;
}

function errorsFor(document: CanonicalRecipeDocument, slug: string, selectedStep?: BuilderStep): FieldError[] {
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
  const catalogSlug = (step: BuilderStep, field: string, value: string, label: string) => {
    if (value && !SLUG.test(value)) add(step, field, `${label} must be 2–63 lowercase letters, numbers, or hyphens.`);
  };

  required(0, "recipe-publisher", document.identity.publisher, "a publisher");
  catalogSlug(0, "recipe-publisher", document.identity.publisher, "Publisher");
  required(0, "recipe-slug", slug, "a recipe slug");
  catalogSlug(0, "recipe-slug", slug, "Recipe slug");
  required(0, "recipe-title", document.metadata.title, "a title");
  required(0, "recipe-description", document.metadata.description, "a description");
  required(0, "model-publisher", document.model.publisher, "a model publisher");
  catalogSlug(0, "model-publisher", document.model.publisher, "Model publisher");
  required(0, "model-slug", document.model.slug, "a model name");
  catalogSlug(0, "model-slug", document.model.slug, "Model name");
  digest(0, "model-digest", document.model.content_sha256, "Model digest");

  required(1, "harness-publisher", document.execution.harness.publisher, "an execution harness publisher");
  catalogSlug(1, "harness-publisher", document.execution.harness.publisher, "Execution harness publisher");
  required(1, "harness-slug", document.execution.harness.slug, "an execution harness name");
  catalogSlug(1, "harness-slug", document.execution.harness.slug, "Execution harness name");
  digest(1, "harness-digest", document.execution.harness.content_sha256, "Execution harness digest");
  if (document.execution.patch_bundle) {
    required(1, "patch-publisher", document.execution.patch_bundle.publisher, "a patch bundle publisher");
    required(1, "patch-slug", document.execution.patch_bundle.slug, "a patch bundle name");
    digest(1, "patch-digest", document.execution.patch_bundle.content_sha256, "Patch bundle digest");
  }
  required(1, "runtime-publisher", document.runtime.distribution.publisher, "a runtime publisher");
  catalogSlug(1, "runtime-publisher", document.runtime.distribution.publisher, "Runtime publisher");
  required(1, "runtime-slug", document.runtime.distribution.slug, "a runtime name");
  catalogSlug(1, "runtime-slug", document.runtime.distribution.slug, "Runtime name");
  digest(1, "runtime-digest", document.runtime.distribution.content_sha256, "Runtime digest");
  if (document.runtime.entrypoint.length === 0) add(1, "runtime-entrypoint", "Add at least one entrypoint argument.");
  required(1, "build-dockerfile", document.build.dockerfile, "a Dockerfile path");
  required(1, "build-platform", document.build.platform, "a target platform");
  required(1, "build-network-mode", document.build.network.mode, "a build network mode");
  digest(1, "context-digest", document.build.context.sha256, "Build context digest");
  required(1, "context-media-type", document.build.context.media_type, "a build context media type");

  if (document.artifacts.length === 0) add(2, "add-artifact", "Add at least one immutable artifact.");
  document.artifacts.forEach((artifact, index) => {
    required(2, `artifact-${index}-id`, artifact.id, `an ID for artifact ${index + 1}`);
    if (artifact.id && !NAME.test(artifact.id)) add(2, `artifact-${index}-id`, `Artifact ${index + 1} name must start with a lowercase letter and use letters, numbers, underscores, or hyphens.`);
    required(2, `artifact-${index}-kind`, artifact.kind, `a kind for artifact ${index + 1}`);
    required(2, `artifact-${index}-repository`, artifact.repository, `a repository for artifact ${index + 1}`);
    required(2, `artifact-${index}-revision`, artifact.revision, `an immutable revision for artifact ${index + 1}`);
    if (artifact.revision === "0123456789abcdef0123456789abcdef01234567") add(2, `artifact-${index}-revision`, `Artifact ${index + 1} revision is still a starter placeholder. Replace it with an immutable commit or content revision.`);
    else if (artifact.revision.length < 40 || artifact.revision.length > 71) add(2, `artifact-${index}-revision`, `Artifact ${index + 1} revision must be 40–71 characters.`);
    integer(2, `artifact-${index}-download`, artifact.download_bytes, `Artifact ${index + 1} download size`, 1);
    integer(2, `artifact-${index}-installed`, artifact.installed_bytes, `Artifact ${index + 1} installed size`, 1);
    required(2, `artifact-${index}-mount`, artifact.mount.target, `a mount target for artifact ${index + 1}`);
    if (artifact.roles.length === 0) add(2, `artifact-${index}-roles`, `Assign artifact ${index + 1} to at least one topology role.`);
  });

  integer(3, "context-size", document.build.context.expected_bytes, "Build context size", 1);
  integer(3, "download-size", document.build.resources.download_bytes, "Additional download size");
  integer(3, "temporary-size", document.build.resources.temporary_bytes, "Temporary storage", 1);
  integer(3, "memory-size", document.build.resources.memory_bytes, "Build memory", 1);
  integer(3, "build-timeout", document.build.resources.timeout_seconds, "Build timeout", 1);
  integer(3, "pre-start-count", document.runtime.lifecycle.pre_start.length, "Pre-start phase count");
  integer(3, "post-stop-count", document.runtime.lifecycle.post_stop.length, "Post-stop phase count");
  integer(3, "stop-timeout", document.runtime.lifecycle.stop_timeout_seconds, "Stop timeout", 1);
  required(3, "topology-name", document.topology.name, "a topology name");
  integer(3, "topology-nodes", document.topology.node_count, "Spark count", 1);
  if (document.topology.roles.length === 0) add(3, "topology-role", "Add at least one topology role in Advanced JSON.");
  const roleNames = new Set(document.topology.roles.map(role => role.name));
  if (document.topology.roles.reduce((sum, role) => sum + role.count, 0) !== document.topology.node_count) add(3, "topology-role", "Topology role counts must equal the Spark count.");
  if (document.topology.roles.filter(role => role.endpoint_owner && role.count === 1).length !== 1) add(3, "topology-role", "Exactly one single-Spark role must own the endpoint.");
  if (document.topology.parallelism.tensor * document.topology.parallelism.pipeline * document.topology.parallelism.data !== document.topology.node_count || document.topology.parallelism.world_size !== document.topology.node_count) add(3, "topology-role", "Topology parallelism and world size must equal the Spark count.");
  if ((document.topology.node_count === 1) !== (document.topology.fabric.connectivity === "none")) add(3, "topology-role", "Only a one-Spark topology may use no fabric connectivity.");
  if (new Set(document.topology.start_order).size !== roleNames.size || document.topology.start_order.some(name => !roleNames.has(name)) || new Set(document.topology.stop_order).size !== roleNames.size || document.topology.stop_order.some(name => !roleNames.has(name))) add(3, "topology-role", "Start and stop order must each name every topology role once.");
  document.artifacts.forEach((artifact, index) => {
    if (artifact.roles.some(role => !roleNames.has(role))) add(2, `artifact-${index}-roles`, `Artifact ${index + 1} names a role that is not in the topology.`);
    const assigned = new Set(document.topology.roles.filter(role => role.artifacts.includes(artifact.id)).map(role => role.name));
    if (artifact.roles.some(role => !assigned.has(role)) || [...assigned].some(role => !artifact.roles.includes(role))) add(2, `artifact-${index}-roles`, `Artifact ${index + 1} role assignments must match the topology.`);
  });
  if (document.interfaces.length === 0) add(3, "add-interface", "Add at least one service interface.");
  const seenAdapters = new Set<InterfaceAdapter>();
  document.interfaces.forEach((item, index) => {
    required(3, `interface-${index}-adapter`, item.adapter, `an adapter for interface ${index + 1}`);
    if (seenAdapters.has(item.adapter)) add(3, `interface-${index}-adapter`, `Interface adapter ${item.adapter} is already declared.`);
    seenAdapters.add(item.adapter);
    if (item.adapter === "openai" && (!Number.isSafeInteger(item.port) || (item.port ?? 0) < 1024 || (item.port ?? 0) > 65535)) {
      add(3, `interface-${index}-port`, `Interface ${index + 1} port must be between 1024 and 65535.`);
    }
    if (item.adapter === "openai" && (item.model_aliases?.length ?? 0) === 0) add(3, `interface-${index}-aliases`, `Interface ${index + 1} needs at least one model alias.`);
    if (item.adapter === "openai" && !item.health_path) add(3, `interface-${index}-health`, `Interface ${index + 1} needs a health path.`);
    if (item.adapter !== "openai" && !item.path) add(3, `interface-${index}-path`, `Interface ${index + 1} needs a job path.`);
  });

  if (document.validation.validators.length === 0) add(4, "validation-checks", "Add at least one validator and check.");
  document.validation.validators.forEach((validator, index) => {
    if (!seenAdapters.has(validator.interface)) add(4, `validator-${index}-interface`, `Validator ${index + 1} must use a declared interface.`);
    if (validator.checks.length === 0) add(4, `validator-${index}-checks`, `Validator ${index + 1} needs at least one check.`);
  });
  return errors;
}

function TextField({id, label, value, onChange, error, helper, type = "text", min, max, readOnly = false, required = false}: {
  id: string; label: string; value: string | number; onChange(value: string): void; error?: string; helper?: string;
  type?: "text" | "number" | "url"; min?: number; max?: number; readOnly?: boolean; required?: boolean;
}) {
  const describedBy = [helper ? `${id}-help` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  return <label htmlFor={id}><span id={`${id}-label`}>{label}</span><input id={id} type={type} min={min} max={max} value={value} readOnly={readOnly} required={required} onChange={event => onChange(event.target.value)} aria-labelledby={`${id}-label`} aria-invalid={error ? true : undefined} aria-describedby={describedBy}/>{helper && <small id={`${id}-help`} className="builder-field-help">{helper}</small>}{error && <span id={`${id}-error`} className="builder-field-error">{error}</span>}</label>;
}

function DigestField(props: Omit<Parameters<typeof TextField>[0], "helper">) {
  return <TextField {...props} required helper="SHA-256 without the sha256: prefix. The repeated-digit starter value cannot be saved."/>;
}

const byteUnits = [
  {label: "B", factor: 1}, {label: "KiB", factor: 1024}, {label: "MiB", factor: MIB}, {label: "GiB", factor: GIB}, {label: "TiB", factor: 1024 ** 4},
] as const;

function preferredUnit(value: number): number {
  for (let index = byteUnits.length - 1; index > 0; index -= 1) if (value >= byteUnits[index].factor && value % byteUnits[index].factor === 0) return index;
  return 0;
}

function HumanBytesField({id, label, value, onChange, error, required = false}: {id: string; label: string; value: number; onChange(value: number): void; error?: string; required?: boolean}) {
  const [unitIndex, setUnitIndex] = useState(() => preferredUnit(value));
  const unit = byteUnits[unitIndex];
  return <fieldset className="builder-unit-field" aria-describedby={error ? `${id}-error` : undefined}>
    <legend>{label}</legend>
    <div><input id={id} aria-label={`${label} amount`} type="number" min="0" step="any" value={value / unit.factor} required={required} aria-invalid={error ? true : undefined} aria-describedby={error ? `${id}-error` : undefined} onChange={event => onChange(Math.round(Number(event.target.value) * unit.factor))}/><select aria-label={`${label} unit`} value={unitIndex} onChange={event => setUnitIndex(Number(event.target.value))}>{byteUnits.map((item, index) => <option key={item.label} value={index}>{item.label}</option>)}</select></div>
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

export function CustomRecipeBuilderPage({api, onNavigate, onBusyChange, onDirtyChange}: {
  api: CatalogApi;
  onNavigate(nextUrl: string): void;
  onBusyChange?(busy: boolean): void;
  onDirtyChange?(dirty: boolean): void;
}) {
  const restored = useMemo(storedBuilderDraft, []);
  const initial = useMemo(() => restored?.document ?? defaultDocument(), [restored]);
  const [document, setDocument] = useState(initial);
  const [slug, setSlug] = useState(restored?.slug ?? initial.identity.slug);
  const [documentText, setDocumentText] = useState(() => restored?.documentText ?? JSON.stringify(initial, null, 2));
  const [step, setStep] = useState<BuilderStep>(restored?.step ?? 0);
  const [highestStep, setHighestStep] = useState<BuilderStep>(restored?.highestStep ?? 0);
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [jsonError, setJsonError] = useState(() => {
    if (!restored) return "";
    const parsed = parseCanonicalRecipeDocument(restored.documentText);
    return parsed.ok ? "" : parsed.error;
  });
  const [preset, setPreset] = useState<PresetName>("custom");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [createdRecipeId, setCreatedRecipeId] = useState("");
  const [dirty, setDirty] = useState(Boolean(restored));
  const [confirmPreset, setConfirmPreset] = useState(false);
  const pageHeading = useRef<HTMLHeadingElement>(null);
  const stepHeading = useRef<HTMLHeadingElement>(null);
  const keepDraft = useRef<HTMLButtonElement>(null);
  const nextArtifactKey = useRef(initial.artifacts.length);
  const artifactKeys = useRef(initial.artifacts.map((_, index) => `artifact-${index}`));

  useEffect(() => { pageHeading.current?.focus(); }, []);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => { onBusyChange?.(false); onDirtyChange?.(false); }, [onBusyChange, onDirtyChange]);
  useEffect(() => {
    if (!dirty || createdRecipeId) {
      discardStoredCustomRecipeDraft();
      return;
    }
    try { sessionStorage.setItem(CUSTOM_RECIPE_DRAFT_STORAGE_KEY, JSON.stringify({version: 1, document, documentText, highestStep, slug, step})); } catch { /* The navigation guard still protects drafts when storage is unavailable. */ }
  }, [createdRecipeId, dirty, document, documentText, highestStep, slug, step]);
  useEffect(() => { if (confirmPreset) keepDraft.current?.focus(); }, [confirmPreset]);

  function update(updater: (current: CanonicalRecipeDocument) => CanonicalRecipeDocument) {
    setDirty(true);
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

  function replaceWithPreset() {
    const next = createCanonicalRecipeDocument(preset);
    update(() => next);
    setSlug(next.identity.slug);
    setConfirmPreset(false);
  }

  function applyPreset() {
    if (dirty) {
      setConfirmPreset(true);
      return;
    }
    replaceWithPreset();
  }

  function goNext() {
    const nextErrors = errorsFor(document, slug, step);
    setErrors(nextErrors);
    if (nextErrors.length > 0) return;
    const nextStep = Math.min(5, step + 1) as BuilderStep;
    setStep(nextStep);
    setHighestStep(current => Math.max(current, nextStep) as BuilderStep);
    queueMicrotask(() => stepHeading.current?.focus());
  }

  function goTo(nextStep: BuilderStep) {
    if (nextStep > highestStep) return;
    setStep(nextStep);
    setErrors([]);
    queueMicrotask(() => stepHeading.current?.focus());
  }

  async function createRecipe() {
    const allErrors = errorsFor(document, slug);
    const parsed = parseCanonicalRecipeDocument(documentText);
    if (!parsed.ok) allErrors.push({field: "advanced-json", message: parsed.error, step: 5});
    setErrors(allErrors);
    if (allErrors.length > 0 || !parsed.ok) return;
    setBusy(true);
    onBusyChange?.(true);
    setStatus("");
    try {
      const created = await api.createCatalogRecipe({slug, document: parsed.document});
      setCreatedRecipeId(created.recipe_id);
      setStatus("Recipe draft saved");
      setDirty(false);
    } catch (value) {
      setStatus(value instanceof Error ? value.message.slice(0, 256) : "Unable to save recipe draft");
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  }

  const error = (field: string) => errors.find(item => item.field === field)?.message;
  const artifactKey = (index: number) => {
    while (artifactKeys.current.length <= index) {
      artifactKeys.current.push(`artifact-${nextArtifactKey.current}`);
      nextArtifactKey.current += 1;
    }
    return artifactKeys.current[index];
  };
  const updateArtifact = (index: number, changes: Partial<CanonicalArtifact>) => update(current => {
    const previous = current.artifacts[index];
    const next = {...previous, ...changes};
    const roles = current.topology.roles.map(role => {
      const withoutPrevious = role.artifacts.filter(id => id !== previous.id && id !== next.id);
      return {...role, artifacts: next.roles.includes(role.name) ? [...withoutPrevious, next.id] : withoutPrevious};
    });
    return {...current, artifacts: current.artifacts.map((item, itemIndex) => itemIndex === index ? next : item), topology: {...current.topology, roles}};
  });
  const removeArtifact = (index: number) => {
    artifactKeys.current.splice(index, 1);
    update(current => {
      const removed = current.artifacts[index];
      return {...current, artifacts: current.artifacts.filter((_, itemIndex) => itemIndex !== index), topology: {...current.topology, roles: current.topology.roles.map(role => ({...role, artifacts: role.artifacts.filter(id => id !== removed.id)}))}};
    });
  };
  const addArtifact = () => {
    artifactKeys.current.push(`artifact-${nextArtifactKey.current}`);
    nextArtifactKey.current += 1;
    update(current => {
      const id = `artifact-${current.artifacts.length + 1}`;
      const roleNames = current.topology.roles.map(role => role.name);
      const artifact: CanonicalArtifact = {id, kind: "huggingface.snapshot", repository: "", revision: "", download_bytes: 1, installed_bytes: 1, mount: {target: `/models/${id}`, read_only: true}, roles: roleNames};
      return {...current, artifacts: [...current.artifacts, artifact], topology: {...current.topology, roles: current.topology.roles.map(role => ({...role, artifacts: [...role.artifacts, id]}))}};
    });
  };
  const replaceInterface = (index: number, adapter: InterfaceAdapter) => update(current => {
    const oldAdapter = current.interfaces[index].adapter;
    return {...current, interfaces: current.interfaces.map((item, itemIndex) => itemIndex === index ? interfaceDefaults(adapter) : item), validation: {...current.validation, validators: current.validation.validators.map(validator => validator.interface === oldAdapter ? {...validator, interface: adapter} : validator)}};
  });
  const updateInterface = (index: number, changes: Partial<CanonicalInterface>) => update(current => ({...current, interfaces: current.interfaces.map((item, itemIndex) => itemIndex === index ? {...item, ...changes} : item)}));
  const removeInterface = (index: number) => update(current => {
    const adapter = current.interfaces[index].adapter;
    return {...current, interfaces: current.interfaces.filter((_, itemIndex) => itemIndex !== index), validation: {...current.validation, validators: current.validation.validators.filter(validator => validator.interface !== adapter)}};
  });

  return <div className="recipe-builder-page">
    <header className="builder-hero">
      <div><p className="fleet-kicker">Local recipe builder</p><h1 ref={pageHeading} tabIndex={-1}>Create custom recipe</h1><p>Build a reviewable recipe one decision at a time. Nothing is saved until the final step.</p></div>
      <button type="button" className="button secondary" disabled={busy} onClick={() => onNavigate("/library")}>Back to Library</button>
    </header>
    {restored && !createdRecipeId && <p className="builder-draft-restored" role="status"><strong>Unsaved draft restored.</strong> Continue where this browser session left off.</p>}

    <nav className="builder-progress" aria-label="Recipe builder progress">
      <ol>{steps.map((item, index) => <li key={item.name} className={index === step ? "current" : index < step ? "complete" : ""}>
        <button type="button" disabled={index > highestStep || busy} aria-current={index === step ? "step" : undefined} onClick={() => goTo(index as BuilderStep)}><span>{index + 1}</span><strong>{item.short}</strong></button>
      </li>)}</ol>
    </nav>

    <section className="builder-workspace" aria-labelledby="builder-step-heading">
      <header><div><span>Step {step + 1} of {steps.length}</span><h2 id="builder-step-heading" ref={stepHeading} tabIndex={-1}>{steps[step].name}</h2><p>{steps[step].description}</p></div><span className="library-panel-badge">Draft</span></header>
      <ErrorSummary errors={errors} onSelect={selected => { setStep(selected.step); setHighestStep(current => Math.max(current, selected.step) as BuilderStep); }}/>
      <fieldset className="builder-form-lock" disabled={busy}>

      {jsonError && <section id="guided-editing-paused" className="builder-json-warning" role="alert"><strong>Guided editing is paused</strong><p>Advanced JSON is invalid. Correct it there before changing guided fields so none of your raw JSON work is discarded.</p></section>}
      <fieldset className="builder-guided-lock" disabled={Boolean(jsonError)} aria-describedby={jsonError ? "guided-editing-paused" : undefined}>
      <legend className="visually-hidden">Guided recipe fields</legend>

      {step === 0 && <div className="builder-step-fields">
        <fieldset className="builder-preset"><legend>Start from a useful shape</legend><p>Presets supply clearly labeled, schema-valid defaults for the artifact, security, topology, endpoint, and validation contracts. Replace every starter digest and revision with exact authority before saving.</p><div><select aria-label="Recipe starting point" value={preset} onChange={event => setPreset(event.target.value as PresetName)}><option value="custom">Custom model service</option><option value="vllm">vLLM chat service</option><option value="diffusers">Diffusers image service</option></select><button type="button" className="button secondary" onClick={applyPreset}>Apply starting point</button></div>{confirmPreset && <section className="builder-preset-confirmation" role="alert" aria-labelledby="replace-draft-title"><div><strong id="replace-draft-title">Replace the current draft?</strong><p>Applying this starting point replaces every unsaved answer in the builder.</p></div><div><button ref={keepDraft} type="button" className="button secondary" onClick={() => setConfirmPreset(false)}>Keep current draft</button><button type="button" className="button danger" onClick={replaceWithPreset}>Replace draft</button></div></section>}</fieldset>
        <fieldset className="custom-recipe-section"><legend>Recipe identity</legend><div className="custom-recipe-fields custom-recipe-fields-two">
          <TextField id="recipe-publisher" label="Publisher" value={document.identity.publisher} required onChange={value => update(current => ({...current, identity: {...current.identity, publisher: value}}))} error={error("recipe-publisher")}/>
          <TextField id="recipe-slug" label="Recipe slug" value={slug} required onChange={changeSlug} error={error("recipe-slug")} helper="Stable URL-safe name used by the local catalog."/>
          <TextField id="recipe-title" label="Display name" value={document.metadata.title} required onChange={value => update(current => ({...current, metadata: {...current.metadata, title: value}}))} error={error("recipe-title")}/>
          <TextField id="recipe-tags" label="Capabilities and tags" value={joinList(document.metadata.tags)} onChange={value => update(current => ({...current, metadata: {...current.metadata, tags: splitList(value)}}))} helper="Comma-separated, for example chat, vision, reasoning."/>
          <label className="custom-recipe-wide" htmlFor="recipe-description">Description<textarea id="recipe-description" rows={3} value={document.metadata.description} required aria-invalid={error("recipe-description") ? true : undefined} aria-describedby={error("recipe-description") ? "recipe-description-error" : undefined} onChange={event => update(current => ({...current, metadata: {...current.metadata, description: event.target.value}}))}/>{error("recipe-description") && <span id="recipe-description-error" className="builder-field-error">{error("recipe-description")}</span>}</label>
        </div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Exact model version</legend><p className="custom-recipe-section-help">Use a friendly publisher and model name here; the digest keeps the version immutable.</p><div className="custom-recipe-fields custom-recipe-fields-two">
          <TextField id="model-publisher" label="Model creator or publisher" value={document.model.publisher} required onChange={value => update(current => ({...current, model: {...current.model, publisher: value}}))} error={error("model-publisher")}/>
          <TextField id="model-slug" label="Model name" value={document.model.slug} required onChange={value => update(current => ({...current, model: {...current.model, slug: value}}))} error={error("model-slug")}/>
          <div className="custom-recipe-wide"><DigestField id="model-digest" label="Exact model digest" value={document.model.content_sha256} onChange={value => update(current => ({...current, model: {...current.model, content_sha256: value}}))} error={error("model-digest")}/></div>
        </div></fieldset>
      </div>}

      {step === 1 && <div className="builder-step-fields">
        <div className="custom-recipe-card-grid">
          <fieldset className="custom-recipe-section"><legend>Execution harness</legend><div className="custom-recipe-fields">
            <TextField id="harness-publisher" label="Publisher" value={document.execution.harness.publisher} required onChange={value => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, publisher: value}}}))} error={error("harness-publisher")}/>
            <TextField id="harness-slug" label="Harness name" value={document.execution.harness.slug} required onChange={value => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, slug: value}}}))} error={error("harness-slug")}/>
            <div className="custom-recipe-wide"><DigestField id="harness-digest" label="Exact harness digest" value={document.execution.harness.content_sha256} onChange={value => update(current => ({...current, execution: {...current.execution, harness: {...current.execution.harness, content_sha256: value}}}))} error={error("harness-digest")}/></div>
          </div></fieldset>
          <fieldset className="custom-recipe-section"><legend>Runtime distribution</legend><div className="custom-recipe-fields">
            <TextField id="runtime-publisher" label="Publisher" value={document.runtime.distribution.publisher} required onChange={value => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, publisher: value}}}))} error={error("runtime-publisher")}/>
            <TextField id="runtime-slug" label="Runtime name" value={document.runtime.distribution.slug} required onChange={value => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, slug: value}}}))} error={error("runtime-slug")}/>
            <div className="custom-recipe-wide"><DigestField id="runtime-digest" label="Exact runtime digest" value={document.runtime.distribution.content_sha256} onChange={value => update(current => ({...current, runtime: {...current.runtime, distribution: {...current.runtime.distribution, content_sha256: value}}}))} error={error("runtime-digest")}/></div>
          </div></fieldset>
        </div>
        <fieldset className="custom-recipe-section"><legend>Process command</legend><div className="custom-recipe-fields custom-recipe-fields-two"><TextField id="runtime-entrypoint" label="Entrypoint arguments" value={joinList(document.runtime.entrypoint)} required onChange={value => update(current => ({...current, runtime: {...current.runtime, entrypoint: splitList(value)}}))} error={error("runtime-entrypoint")} helper="Comma-separated arguments, in execution order."/></div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Build contract</legend><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="build-dockerfile" label="Dockerfile path" value={document.build.dockerfile} required onChange={value => update(current => ({...current, build: {...current.build, dockerfile: value}}))} error={error("build-dockerfile")}/>
          <TextField id="build-platform" label="Target platform" value={document.build.platform} onChange={() => undefined} readOnly required error={error("build-platform")} helper="Recipe v1 currently requires linux/arm64."/>
          <label htmlFor="build-network-mode">Build network mode<select id="build-network-mode" required value={document.build.network.mode} onChange={event => update(current => ({...current, build: {...current.build, network: {...current.build.network, mode: event.target.value as "none" | "public"}}}))}><option value="none">No build network</option><option value="public">Public allowlist</option></select></label>
          <TextField id="context-media-type" label="Build context media type" value={document.build.context.media_type} onChange={() => undefined} readOnly required error={error("context-media-type")} helper="Canonical Vonk Forge source bundle."/>
          <TextField id="network-hosts" label="Allowed network hosts" value={joinList(document.build.network.hosts)} onChange={value => update(current => ({...current, build: {...current.build, network: {...current.build.network, hosts: splitList(value)}}}))} helper="Comma-separated. Used only with the public allowlist."/>
          <div className="custom-recipe-wide"><DigestField id="context-digest" label="Exact build context digest" value={document.build.context.sha256} onChange={value => update(current => ({...current, build: {...current.build, context: {...current.build.context, sha256: value}}}))} error={error("context-digest")}/></div>
        </div><p className="custom-recipe-section-help">Build arguments and an optional target remain available in Advanced JSON.</p></fieldset>
        <label className="custom-recipe-checkbox"><input type="checkbox" checked={document.execution.patch_bundle !== null} onChange={event => update(current => ({...current, execution: {...current.execution, patch_bundle: event.target.checked ? {kind: "patch-bundle", publisher: "local", slug: "patch", content_sha256: "4".repeat(64)} : null}}))}/> Add an immutable patch bundle</label>
        {document.execution.patch_bundle && <fieldset className="custom-recipe-section"><legend>Patch bundle</legend><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="patch-publisher" label="Publisher" value={document.execution.patch_bundle.publisher} onChange={value => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, publisher: value} : null}}))} error={error("patch-publisher")}/>
          <TextField id="patch-slug" label="Patch name" value={document.execution.patch_bundle.slug} onChange={value => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, slug: value} : null}}))} error={error("patch-slug")}/>
          <DigestField id="patch-digest" label="Exact patch digest" value={document.execution.patch_bundle.content_sha256} onChange={value => update(current => ({...current, execution: {...current.execution, patch_bundle: current.execution.patch_bundle ? {...current.execution.patch_bundle, content_sha256: value} : null}}))} error={error("patch-digest")}/>
        </div></fieldset>}
      </div>}

      {step === 2 && <div className="builder-step-fields">
        <div className="builder-step-callout"><div><strong>{document.artifacts.length}</strong><span>{document.artifacts.length === 1 ? "artifact" : "artifacts"}</span></div><p>At least one immutable artifact is required. Record its exact origin, mount, and every topology role that receives it.</p></div>
        <div className="custom-recipe-repeat-list">
          {document.artifacts.map((artifact, index) => <fieldset className="custom-recipe-section" key={artifactKey(index)}><legend>Artifact {index + 1}</legend><div className="custom-recipe-card-heading"><p className="custom-recipe-section-help">Preset values are starters; verify the repository and immutable revision before saving.</p><button type="button" className="button secondary" onClick={() => removeArtifact(index)}>Remove artifact {index + 1}</button></div><div className="custom-recipe-fields custom-recipe-fields-two">
            <TextField id={`artifact-${index}-id`} label="Artifact name" value={artifact.id} required onChange={value => updateArtifact(index, {id: value})} error={error(`artifact-${index}-id`)}/>
            <label htmlFor={`artifact-${index}-kind`}>Source type<select id={`artifact-${index}-kind`} required value={artifact.kind} onChange={event => updateArtifact(index, {kind: event.target.value as CanonicalArtifact["kind"]})}><option value="huggingface.snapshot">Hugging Face snapshot</option><option value="http.file">HTTP file</option><option value="oci.artifact">OCI artifact</option></select></label>
            <TextField id={`artifact-${index}-repository`} label="Original repository" value={artifact.repository} required onChange={value => updateArtifact(index, {repository: value})} error={error(`artifact-${index}-repository`)}/>
            <TextField id={`artifact-${index}-revision`} label="Immutable revision" value={artifact.revision} required onChange={value => updateArtifact(index, {revision: value})} error={error(`artifact-${index}-revision`)}/>
            <HumanBytesField id={`artifact-${index}-download`} label="Download size" value={artifact.download_bytes} required onChange={value => updateArtifact(index, {download_bytes: value})} error={error(`artifact-${index}-download`)}/>
            <HumanBytesField id={`artifact-${index}-installed`} label="Installed size" value={artifact.installed_bytes} required onChange={value => updateArtifact(index, {installed_bytes: value})} error={error(`artifact-${index}-installed`)}/>
            <TextField id={`artifact-${index}-mount`} label="Container mount" value={artifact.mount.target} required onChange={value => updateArtifact(index, {mount: {...artifact.mount, target: value}})} error={error(`artifact-${index}-mount`)}/>
            <label className="custom-recipe-checkbox"><input type="checkbox" checked={artifact.mount.read_only} onChange={event => updateArtifact(index, {mount: {...artifact.mount, read_only: event.target.checked}})}/> Mount read-only</label>
            <TextField id={`artifact-${index}-roles`} label="Topology roles" value={joinList(artifact.roles)} required onChange={value => updateArtifact(index, {roles: splitList(value)})} error={error(`artifact-${index}-roles`)} helper={`Comma-separated. Current roles: ${document.topology.roles.map(role => role.name).join(", ") || "none"}.`}/>
          </div></fieldset>)}
          <button id="add-artifact" type="button" className="button secondary custom-recipe-add" onClick={addArtifact}>Add artifact</button>
        </div>
      </div>}

      {step === 3 && <div className="builder-step-fields">
        <div className="builder-resource-summary" aria-label="Resource envelope"><div><span>Context</span><strong>{formatBytes(document.build.context.expected_bytes)}</strong></div><div><span>Download</span><strong>{formatBytes(document.build.resources.download_bytes + document.artifacts.reduce((total, item) => total + item.download_bytes, 0))}</strong></div><div><span>Temporary</span><strong>{formatBytes(document.build.resources.temporary_bytes)}</strong></div><div><span>Build memory</span><strong>{formatBytes(document.build.resources.memory_bytes)}</strong></div></div>
        <fieldset className="custom-recipe-section"><legend>Resource envelope</legend><p className="custom-recipe-section-help">Enter normal human units; the recipe is saved using exact bytes.</p><div className="builder-unit-grid">
          <HumanBytesField id="context-size" label="Build context" value={document.build.context.expected_bytes} onChange={value => update(current => ({...current, build: {...current.build, context: {...current.build.context, expected_bytes: value}}}))} error={error("context-size")}/>
          <HumanBytesField id="download-size" label="Additional downloads" value={document.build.resources.download_bytes} onChange={value => update(current => ({...current, build: {...current.build, resources: {...current.build.resources, download_bytes: value}}}))} error={error("download-size")}/>
          <HumanBytesField id="temporary-size" label="Temporary storage" value={document.build.resources.temporary_bytes} onChange={value => update(current => ({...current, build: {...current.build, resources: {...current.build.resources, temporary_bytes: value}}}))} error={error("temporary-size")}/>
          <HumanBytesField id="memory-size" label="Build memory" value={document.build.resources.memory_bytes} onChange={value => update(current => ({...current, build: {...current.build, resources: {...current.build.resources, memory_bytes: value}}}))} error={error("memory-size")}/>
        </div><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="build-timeout" label="Build timeout (seconds)" type="number" min={1} value={document.build.resources.timeout_seconds} onChange={value => update(current => ({...current, build: {...current.build, resources: {...current.build.resources, timeout_seconds: Number(value)}}}))} error={error("build-timeout")}/>
          <TextField id="pre-start-count" label="Pre-start commands" type="number" min={0} value={document.runtime.lifecycle.pre_start.length} onChange={value => update(current => ({...current, runtime: {...current.runtime, lifecycle: {...current.runtime.lifecycle, pre_start: resizeCommands(current.runtime.lifecycle.pre_start, Number(value), "pre-start")}}}))} error={error("pre-start-count")}/>
          <TextField id="post-stop-count" label="Post-stop commands" type="number" min={0} value={document.runtime.lifecycle.post_stop.length} onChange={value => update(current => ({...current, runtime: {...current.runtime, lifecycle: {...current.runtime.lifecycle, post_stop: resizeCommands(current.runtime.lifecycle.post_stop, Number(value), "post-stop")}}}))} error={error("post-stop-count")}/>
          <TextField id="stop-timeout" label="Stop timeout (seconds)" type="number" min={1} value={document.runtime.lifecycle.stop_timeout_seconds} onChange={value => update(current => ({...current, runtime: {...current.runtime, lifecycle: {...current.runtime.lifecycle, stop_timeout_seconds: Number(value)}}}))} error={error("stop-timeout")}/>
        </div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Topology</legend><p className="custom-recipe-section-help">Changing the Spark count creates an explicit endpoint-owner role and, for multi-Spark recipes, a worker role. Verify preset role resources below or edit the complete contract in Advanced JSON.</p><div className="custom-recipe-fields custom-recipe-fields-three">
          <TextField id="topology-name" label="Topology name" value={document.topology.name} onChange={value => update(current => ({...current, topology: {...current.topology, name: value}}))} error={error("topology-name")}/>
          <TextField id="topology-nodes" label="Sparks" type="number" min={1} value={document.topology.node_count} onChange={value => update(current => topologyWithNodeCount(current, Number(value)))} error={error("topology-nodes")}/>
          <label htmlFor="topology-mode">Execution mode<select id="topology-mode" value={document.topology.mode} onChange={event => update(current => ({...current, topology: {...current.topology, mode: event.target.value as TopologyMode}}))}>{["single", "distributed", "tensor_parallel", "pipeline_parallel", "data_parallel", "hybrid", "ray", "mpi"].map(value => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
          <TextField id="topology-backend" label="Parallel backend" value={document.topology.parallelism.backend} onChange={value => update(current => ({...current, topology: {...current.topology, parallelism: {...current.topology.parallelism, backend: value}}}))}/>
          <TextField id="topology-bandwidth" label="Minimum fabric bandwidth (Mbps)" type="number" min={0} value={document.topology.fabric.minimum_bandwidth_mbps} onChange={value => update(current => ({...current, topology: {...current.topology, fabric: {...current.topology.fabric, minimum_bandwidth_mbps: Number(value)}}}))}/>
        </div><div id="topology-role" className="custom-recipe-repeat-list">{document.topology.roles.map(role => <div className="custom-recipe-card" key={role.name}><strong>{role.name}</strong><span>{role.count} {role.count === 1 ? "Spark" : "Sparks"}{role.endpoint_owner ? " · endpoint owner" : ""}</span><small>{formatBytes(role.resources.memory.startup_peak_bytes)} startup memory · {formatBytes(role.resources.disk.image_bytes + role.resources.disk.artifact_bytes + role.resources.disk.staging_bytes + role.resources.disk.cache_bytes + role.resources.disk.rollback_bytes + role.resources.disk.safety_margin_bytes)} disk envelope</small></div>)}</div>{error("topology-role") && <span className="builder-field-error">{error("topology-role")}</span>}</fieldset>
        <div className="builder-topology-preview" aria-label="Service topology preview"><div><span>Model</span><strong>{document.model.publisher}/{document.model.slug}</strong></div><span aria-hidden="true">→</span><div><span>Topology</span><strong>{document.topology.node_count} {document.topology.node_count === 1 ? "Spark" : "Sparks"}</strong></div><span aria-hidden="true">→</span><div><span>Endpoints</span><strong>{document.interfaces.length || "None"}</strong></div></div>
        <fieldset className="custom-recipe-section"><legend>Exposed interfaces</legend><p className="custom-recipe-section-help">These endpoints describe how operators and clients reach the running topology.</p><div className="custom-recipe-repeat-list">
          {document.interfaces.map((item, index) => <div className="custom-recipe-card" key={`${item.adapter}-${index}`}><div className="custom-recipe-card-heading"><h4>Interface {index + 1}</h4><button type="button" className="button secondary" onClick={() => removeInterface(index)}>Remove interface {index + 1}</button></div><div className="custom-recipe-fields custom-recipe-fields-three">
            <label htmlFor={`interface-${index}-adapter`}>Adapter<select id={`interface-${index}-adapter`} required value={item.adapter} onChange={event => replaceInterface(index, event.target.value as InterfaceAdapter)} aria-invalid={error(`interface-${index}-adapter`) ? true : undefined} aria-describedby={error(`interface-${index}-adapter`) ? `interface-${index}-adapter-error` : undefined}><option value="openai">OpenAI API</option><option value="image-job">Image job</option><option value="audio-job">Audio job</option><option value="video-job">Video job</option><option value="mesh-job">Mesh job</option><option value="artifact-job">Artifact job</option></select>{error(`interface-${index}-adapter`) && <span id={`interface-${index}-adapter-error`} className="builder-field-error">{error(`interface-${index}-adapter`)}</span>}</label>
            {item.adapter === "openai" ? <>
              <TextField id={`interface-${index}-port`} label="Port" type="number" min={1024} max={65535} value={item.port ?? ""} onChange={value => updateInterface(index, {port: value ? Number(value) : undefined})} error={error(`interface-${index}-port`)}/>
              <TextField id={`interface-${index}-health`} label="Health path" value={item.health_path ?? ""} onChange={value => updateInterface(index, {health_path: value || undefined})} error={error(`interface-${index}-health`)}/>
              <TextField id={`interface-${index}-aliases`} label="Model aliases" value={joinList(item.model_aliases ?? [])} onChange={value => updateInterface(index, {model_aliases: splitList(value)})} error={error(`interface-${index}-aliases`)}/>
            </> : <TextField id={`interface-${index}-path`} label="Job path" value={item.path ?? ""} onChange={value => updateInterface(index, {path: value || undefined})} error={error(`interface-${index}-path`)}/>}
          </div></div>)}
          <button id="add-interface" type="button" className="button secondary custom-recipe-add" onClick={() => update(current => ({...current, interfaces: [...current.interfaces, interfaceDefaults((["openai", "image-job", "audio-job", "video-job", "mesh-job", "artifact-job"] as InterfaceAdapter[]).find(adapter => !current.interfaces.some(item => item.adapter === adapter)) ?? "openai")]}))}>Add interface</button>
        </div></fieldset>
      </div>}

      {step === 4 && <div className="builder-step-fields">
        <fieldset className="custom-recipe-section"><legend>Validation evidence</legend><p className="custom-recipe-section-help">Every recipe needs at least one validator bound to a declared interface. These are checks to run, not proof that they already passed.</p><div className="custom-recipe-repeat-list">
          {document.validation.validators.map((validator, index) => <div className="custom-recipe-card" key={`${validator.interface}-${index}`}><div className="custom-recipe-card-heading"><h4>Validator {index + 1}</h4><button type="button" className="button secondary" onClick={() => update(current => ({...current, validation: {...current.validation, validators: current.validation.validators.filter((_, itemIndex) => itemIndex !== index)}}))}>Remove validator {index + 1}</button></div><div className="custom-recipe-fields custom-recipe-fields-two">
            <label htmlFor={`validator-${index}-interface`}>Declared interface<select id={`validator-${index}-interface`} required value={validator.interface} onChange={event => update(current => ({...current, validation: {...current.validation, validators: current.validation.validators.map((item, itemIndex) => itemIndex === index ? {...item, interface: event.target.value as InterfaceAdapter} : item)}}))} aria-invalid={error(`validator-${index}-interface`) ? true : undefined} aria-describedby={error(`validator-${index}-interface`) ? `validator-${index}-interface-error` : undefined}>{document.interfaces.map(item => <option key={item.adapter} value={item.adapter}>{item.adapter}</option>)}</select>{error(`validator-${index}-interface`) && <span id={`validator-${index}-interface-error`} className="builder-field-error">{error(`validator-${index}-interface`)}</span>}</label>
            <TextField id={`validator-${index}-checks`} label="Checks" value={joinList(validator.checks)} required onChange={value => update(current => ({...current, validation: {...current.validation, validators: current.validation.validators.map((item, itemIndex) => itemIndex === index ? {...item, checks: splitList(value)} : item)}}))} error={error(`validator-${index}-checks`)} helper="Comma-separated, for example endpoint.healthy, chat.completion."/>
          </div></div>)}
          <button id="validation-checks" type="button" className="button secondary custom-recipe-add" disabled={document.interfaces.length === 0} onClick={() => update(current => ({...current, validation: {...current.validation, validators: [...current.validation.validators, {interface: current.interfaces[0].adapter, checks: ["endpoint.healthy"]}]}}))}>Add validator</button>
        </div><div className="custom-recipe-fields custom-recipe-fields-two"><TextField id="benchmark-count" label="Benchmark definitions" type="number" min={0} value={document.validation.benchmarks.length} onChange={value => update(current => { const count = Math.max(0, Math.trunc(Number(value))); const benchmarks = current.validation.benchmarks.slice(0, count); while (benchmarks.length < count) benchmarks.push({name: `benchmark-${benchmarks.length + 1}`, framework: "custom", configuration: {}}); return {...current, validation: {...current.validation, benchmarks}}; })} helper="Creates clearly labeled starter benchmark definitions; edit their complete configuration in Advanced JSON."/></div></fieldset>
        <fieldset className="custom-recipe-section"><legend>Provenance</legend><p className="custom-recipe-section-help">Record the origin in operator-friendly terms; immutable technical identities stay available in the review.</p><div className="custom-recipe-fields custom-recipe-fields-two">
          <label htmlFor="source-kind">Source kind<select id="source-kind" value={document.provenance.source_kind} onChange={event => update(current => ({...current, provenance: {...current.provenance, source_kind: event.target.value as CanonicalRecipeDocument["provenance"]["source_kind"]}}))}><option value="local">Created locally</option><option value="workload_run">Derived from a workload run</option><option value="global">Imported from a public source</option><option value="fork">Forked from another recipe</option></select></label>
          <TextField id="source-reference" label="Original source or repository" value={document.provenance.source_reference ?? ""} onChange={value => update(current => ({...current, provenance: {...current.provenance, source_reference: value || null}}))} helper="URL, recipe reference, or workload run name."/>
          <div className="custom-recipe-wide"><TextField id="source-attribution" label="Creator and attribution" value={joinList(document.provenance.attribution)} onChange={value => update(current => ({...current, provenance: {...current.provenance, attribution: splitList(value)}}))} helper="Comma-separated names or organizations."/></div>
        </div></fieldset>
      </div>}

      {step === 5 && <section className="builder-review" aria-label="Recipe builder review">
        <section className="builder-preflight" aria-labelledby="builder-preflight-heading">
          <header><div><span aria-hidden="true">✓</span><h3 id="builder-preflight-heading">Draft preflight</h3></div></header>
          <p>This review checks that the draft declares the inputs needed for later source upload and policy validation. It does not upload or execute source code.</p>
          <ul>
            <li><span>Build context digest</span><strong>Declared</strong><code>{document.build.context.sha256}</code></li>
            <li><span>Immutable artifacts</span><strong>{document.artifacts.length} declared</strong><small>{document.artifacts.length ? document.artifacts.map(item => item.id).join(", ") : "None"}</small></li>
            <li><span>Topology</span><strong>{document.topology.node_count} {document.topology.node_count === 1 ? "Spark" : "Sparks"}</strong><small>{document.topology.roles.map(role => role.name).join(", ") || "No roles"}</small></li>
            <li><span>Validators</span><strong>{document.validation.validators.length} declared</strong><small>{document.validation.validators.flatMap(item => item.checks).join(", ") || "No checks"}</small></li>
            <li className="builder-preflight-pending"><span>Source bundle</span><strong>Not uploaded by this builder</strong><small>Upload the bundle matching the declared digest and pass policy checks before build or resolve.</small></li>
          </ul>
        </section>
        <section><header><div><span>1</span><h4>Identity & model</h4></div><button type="button" className="button secondary" aria-label="Change identity and model" onClick={() => goTo(0)}>Change</button></header><dl><ReviewRow term="Recipe">{document.metadata.title}</ReviewRow><ReviewRow term="Catalog name">{document.identity.publisher}/{slug}</ReviewRow><ReviewRow term="Model">{document.model.publisher}/{document.model.slug}</ReviewRow><ReviewRow term="Description">{document.metadata.description}</ReviewRow></dl></section>
        <section><header><div><span>2</span><h4>Runtime</h4></div><button type="button" className="button secondary" aria-label="Change runtime" onClick={() => goTo(1)}>Change</button></header><dl><ReviewRow term="Execution harness">{document.execution.harness.publisher}/{document.execution.harness.slug}</ReviewRow><ReviewRow term="Runtime">{document.runtime.distribution.publisher}/{document.runtime.distribution.slug}</ReviewRow><ReviewRow term="Entrypoint"><code>{document.runtime.entrypoint.join(" ")}</code></ReviewRow><ReviewRow term="Build">{document.build.platform} · {document.build.dockerfile} · network {document.build.network.mode}</ReviewRow></dl></section>
        <section><header><div><span>3</span><h4>Artifacts</h4></div><button type="button" className="button secondary" aria-label="Change artifacts" onClick={() => goTo(2)}>Change</button></header><dl><ReviewRow term="Artifacts">{document.artifacts.length}</ReviewRow><ReviewRow term="Total download">{formatBytes(document.artifacts.reduce((total, item) => total + item.download_bytes, 0))}</ReviewRow><ReviewRow term="Repositories">{document.artifacts.length ? document.artifacts.map(item => item.repository).join(", ") : "None"}</ReviewRow></dl></section>
        <section><header><div><span>4</span><h4>Resources & topology</h4></div><button type="button" className="button secondary" aria-label="Change resources and topology" onClick={() => goTo(3)}>Change</button></header><dl><ReviewRow term="Build memory">{formatBytes(document.build.resources.memory_bytes)}</ReviewRow><ReviewRow term="Temporary storage">{formatBytes(document.build.resources.temporary_bytes)}</ReviewRow><ReviewRow term="Topology">{document.topology.name} · {document.topology.node_count} {document.topology.node_count === 1 ? "Spark" : "Sparks"} · {document.topology.roles.map(role => role.name).join(", ")}</ReviewRow><ReviewRow term="Endpoints">{document.interfaces.length ? document.interfaces.map(item => `${item.adapter}${item.port ? ` :${item.port}` : item.path ? ` ${item.path}` : ""}`).join(", ") : "None"}</ReviewRow><ReviewRow term="Lifecycle">{document.runtime.lifecycle.pre_start.length} pre-start · {document.runtime.lifecycle.post_stop.length} post-stop</ReviewRow></dl></section>
        <section><header><div><span>5</span><h4>Validation & provenance</h4></div><button type="button" className="button secondary" aria-label="Change validation and provenance" onClick={() => goTo(4)}>Change</button></header><dl><ReviewRow term="Checks">{document.validation.validators.flatMap(item => item.checks).join(", ") || "None recorded"}</ReviewRow><ReviewRow term="Benchmarks">{document.validation.benchmarks.length}</ReviewRow><ReviewRow term="Origin">{document.provenance.source_kind.replaceAll("_", " ")}</ReviewRow><ReviewRow term="Attribution">{document.provenance.attribution.join(", ") || "None recorded"}</ReviewRow></dl></section>
        <details className="builder-technical-review"><summary>Technical identities and digests</summary><dl><ReviewRow term="Model digest"><code>{document.model.content_sha256}</code></ReviewRow><ReviewRow term="Harness digest"><code>{document.execution.harness.content_sha256}</code></ReviewRow><ReviewRow term="Runtime digest"><code>{document.runtime.distribution.content_sha256}</code></ReviewRow><ReviewRow term="Build context digest"><code>{document.build.context.sha256}</code></ReviewRow></dl></details>
      </section>}

      </fieldset>
      <details className="library-json-fallback"><summary>Advanced JSON</summary><div className="library-json-fallback-content"><p>Edit or paste every field of the canonical recipe-v1 document, including parameters, arguments, environment, security, lifecycle commands, role resources, and benchmark configuration. Structurally valid JSON immediately updates the guided steps; final backend contract checks still apply when saved.</p><label htmlFor="advanced-json">Recipe document<textarea id="advanced-json" aria-label="Recipe document" rows={12} spellCheck={false} required value={documentText} aria-invalid={jsonError ? true : undefined} aria-describedby={jsonError ? "advanced-json-error" : undefined} onChange={event => { const value = event.target.value; setDirty(true); setDocumentText(value); const parsed = parseCanonicalRecipeDocument(value); if (parsed.ok) { setDocument(parsed.document); setSlug(parsed.document.identity.slug); setJsonError(""); setErrors([]); } else setJsonError(parsed.error); }}/>{jsonError && <span id="advanced-json-error" className="builder-field-error">{jsonError}</span>}</label></div></details>
      </fieldset>

      <footer className="builder-actions">
        <button type="button" className="button secondary" disabled={step === 0 || busy} onClick={() => goTo((step - 1) as BuilderStep)}>Previous</button>
        <span aria-live="polite">{status && <strong role={createdRecipeId ? "status" : "alert"} className={createdRecipeId ? "builder-success" : "builder-save-error"}>{status}</strong>}</span>
        <div>{createdRecipeId && <button type="button" className="button secondary" onClick={() => onNavigate(`/library/recipes/${encodeURIComponent(createdRecipeId)}`)}>View saved draft</button>}{step < 5 ? <button type="button" className="button" onClick={goNext}>Continue</button> : <button type="button" className="button" disabled={busy || Boolean(createdRecipeId) || Boolean(jsonError)} aria-describedby="builder-save-draft-help" onClick={() => void createRecipe()}>{busy ? "Saving draft…" : "Save recipe draft"}</button>}</div>
      </footer>
      {step === 5 && <p id="builder-save-draft-help" className="builder-save-draft-help">Saves this canonical document as a local draft. Source bundle upload and policy checks remain required before build or resolve.</p>}
    </section>
  </div>;
}

import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {
  ArtifactJob,
  ArtifactJobCapabilities,
  ArtifactJobCreateInput,
  ArtifactJobFile,
  ArtifactJobInputFile,
  ArtifactJobInterface,
  LibraryApi,
  LibraryViewRecipeDetail,
  RecipeDefinition,
} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {humanizeIdentifier, TechnicalDetails} from "./library-technical-details";
import {StatusPill} from "./status-pill";
import {hashArtifactBlob} from "./artifact-hash";
import "./artifact-job-workspace.css";

const CONTROLLER_FILE_LIMIT = 512 * 1024 * 1024;
const CONTROLLER_TOTAL_LIMIT = 1024 * 1024 * 1024;
const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

type JobRecipeDocument = RecipeDefinition;
type JobRecipeInterface = Extract<JobRecipeDocument["interfaces"][number], {adapter: ArtifactJobInterface}>;
type RecipeInput = NonNullable<JobRecipeInterface["input"]>;
type RecipeInputSlot = NonNullable<RecipeInput["slots"]>[number];
type RecipeOutputSlot = JobRecipeInterface["output"]["slots"][number];
type Scalar = NonNullable<Extract<JobRecipeDocument["settings"], {kind: "job"}>["knobs"]>[string]["value"];
type RecipeParameter = {name: string; description: string; type: "boolean" | "integer" | "string"; default: Scalar};
type InputPayload = {declaration: ArtifactJobInputFile; blob: Blob};
type CancelRecovery = {jobId: string; runId: string; reason: string; requestId: string; source: "upload" | "history"; message: string};

function responseStatus(value: unknown): number | undefined {
  if (typeof value !== "object" || value === null || !("status" in value)) return undefined;
  return typeof value.status === "number" ? value.status : undefined;
}

function mayHaveBeenAccepted(value: unknown): boolean {
  const status = responseStatus(value);
  return status === undefined || status === 0 || status >= 500;
}

function isDefinitiveHttpRefusal(value: unknown): boolean {
  const status = responseStatus(value);
  return status !== undefined && status >= 400 && status < 500;
}

function isAbort(value: unknown): boolean {
  return (typeof DOMException !== "undefined" && value instanceof DOMException && value.name === "AbortError")
    || (value instanceof Error && value.name === "AbortError");
}

function assertJobIdentity(job: ArtifactJob, expectedJobId: string | undefined, expectedRunId: string, source: string): void {
  if (expectedJobId && job.id !== expectedJobId) {
    throw new Error(`${source} returned another artifact job; expected ${expectedJobId}`);
  }
  if (job.run_id !== expectedRunId) {
    throw new Error(`${source} belongs to another recipe run; expected ${expectedRunId}`);
  }
}

function errorText(value: unknown, fallback: string): string {
  return value instanceof Error ? value.message.slice(0, 256) : fallback;
}

function cancelReceipt(job: ArtifactJob): {requestId: string; reason: string} | undefined {
  const evidence = job.result_evidence;
  if (!evidence || typeof evidence !== "object") return undefined;
  const requestId = evidence.cancel_request_id;
  const reason = evidence.cancel_reason;
  return typeof requestId === "string" && typeof reason === "string"
    ? {requestId, reason}
    : undefined;
}

function isCancelReceipt(job: ArtifactJob, requestId: string, reason: string): boolean {
  const receipt = cancelReceipt(job);
  return receipt?.requestId === requestId && receipt.reason === reason
    && (job.state === "cancelling" || job.state === "cancelled");
}

function recoveredInputIndexes(job: ArtifactJob, prepared: InputPayload[]): Set<number> {
  const matched = new Set<number>();
  for (const uploaded of job.input_files) {
    const index = prepared.findIndex(({declaration}) => declaration.slot === uploaded.slot && declaration.name === uploaded.name);
    const expected = index < 0 ? undefined : prepared[index]?.declaration;
    if (!expected || matched.has(index)
      || expected.media_type !== uploaded.media_type
      || expected.size_bytes !== uploaded.size_bytes
      || expected.sha256 !== uploaded.sha256) {
      throw new Error(`Durable job ${job.id} has an input that does not match the selected files; its exact draft cannot be reused.`);
    }
    matched.add(index);
  }
  return matched;
}

function abortError(): DOMException {
  return new DOMException("Artifact job submission was cancelled", "AbortError");
}

const outputMedia: Record<ArtifactJobInterface, string[]> = {
  "image-job": ["image/png", "image/jpeg", "image/webp"],
  "audio-job": ["audio/wav", "audio/mpeg", "audio/flac"],
  "video-job": ["video/mp4", "video/webm"],
  "mesh-job": ["model/gltf-binary", "model/gltf+json"],
  "artifact-job": ["application/json", "text/plain", "application/octet-stream", "application/pdf", "image/png", "image/jpeg", "image/webp"],
};

function jobTone(state: ArtifactJob["state"]): "healthy" | "warning" | "danger" | "info" {
  if (state === "succeeded") return "healthy";
  if (state === "failed" || state === "cancelled") return "danger";
  if (state === "running" || state === "queued") return "info";
  return "warning";
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
}

function filename(value: string): string {
  const clean = value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return (clean || "input").slice(0, 128);
}

const extensionMediaTypes: Record<string, string> = {
  ".flac": "audio/flac",
  ".glb": "model/gltf-binary",
  ".gltf": "model/gltf+json",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".json": "application/json",
  ".mp3": "audio/mpeg",
  ".mp4": "video/mp4",
  ".mkv": "video/x-matroska",
  ".mov": "video/quicktime",
  ".obj": "model/obj",
  ".pdf": "application/pdf",
  ".ply": "model/ply",
  ".png": "image/png",
  ".txt": "text/plain",
  ".wav": "audio/wav",
  ".webm": "video/webm",
  ".webp": "image/webp",
};

function fileMediaType(slot: RecipeInputSlot, file: File): string {
  if (slot.media_types.includes(file.type)) return file.type;
  const extension = slot.extensions.find(item => file.name.toLowerCase().endsWith(item.toLowerCase()));
  const inferred = extension ? extensionMediaTypes[extension.toLowerCase()] : undefined;
  if (inferred && slot.media_types.includes(inferred)) return inferred;
  return slot.media_types.length === 1 ? slot.media_types[0] : file.type;
}

function parameterDefaults(parameters: RecipeParameter[]): Record<string, Scalar> {
  return Object.fromEntries(parameters.map(parameter => [parameter.name, parameter.default]));
}

function parameterError(parameter: RecipeParameter, value: Scalar): string | undefined {
  if (value === null) return undefined;
  if (parameter.type === "integer") {
    if (!Number.isInteger(value)) return "Enter a whole number.";
  }
  return undefined;
}

function recipeParameters(document: JobRecipeDocument): RecipeParameter[] {
  if (document.settings.kind !== "job") return [];
  return Object.entries(document.settings.knobs ?? {}).map(([name, setting]) => ({
    name,
    description: "Declared runtime setting.",
    type: typeof setting.value === "boolean" ? "boolean" : typeof setting.value === "number" && Number.isInteger(setting.value) ? "integer" : "string",
    default: setting.value,
  }));
}

function OutputPreview({api, file, job}: {api: LibraryApi; file: ArtifactJobFile; job: ArtifactJob}) {
  const url = api.artifactJobResultUrl(job.id, file.name, file.sha256);
  const label = `${file.name}, ${file.media_type}, ${formatBytes(file.size_bytes)}`;
  return <li className="artifact-output-row">
    <div className="artifact-output-heading"><div><strong>{file.name}</strong><span>{file.media_type} · {formatBytes(file.size_bytes)}</span></div><a className="button secondary" href={url} download={file.name}>Download</a></div>
    {file.media_type.startsWith("image/") && <img src={url} alt={`Generated output ${file.name}`} loading="lazy"/>}
    {file.media_type.startsWith("audio/") && <audio controls preload="metadata" aria-label={`Listen to ${label}`} src={url}/>}
    {file.media_type.startsWith("video/") && <video controls preload="metadata" aria-label={`Watch ${label}`} src={url}/>}
    {(file.media_type === "model/gltf-binary" || file.name.toLowerCase().endsWith(".glb")) && <div className="artifact-glb-notice"><strong>3D artifact ready</strong><span>This browser has no trusted GLB renderer installed. Download the exact result to inspect it without sending it to a third party.</span></div>}
    <TechnicalDetails compact items={[{label: "Output SHA-256", value: file.sha256}]}/>
  </li>;
}

function JobHistory({api, busyJobId, cancelCandidate, job, onCancel, onConfirmCancel, onPrepareRetry}: {
  api: LibraryApi;
  busyJobId?: string;
  cancelCandidate?: string;
  job: ArtifactJob;
  onCancel(jobId?: string): void;
  onConfirmCancel(job: ArtifactJob): void;
  onPrepareRetry(job: ArtifactJob): void;
}) {
  const active = !TERMINAL_STATES.has(job.state);
  return <article className="artifact-job-history-row" aria-label={`Artifact job ${job.id}, ${job.state}`}>
    <header><div><StatusPill tone={jobTone(job.state)}>{humanizeIdentifier(job.state)}</StatusPill><strong>{humanizeIdentifier(job.interface)}</strong><span>{formatDate(job.created_at)}</span></div><span>{formatBytes(job.input_total_bytes)} input</span></header>
    {active && <div className="artifact-job-progress" role="status" aria-live="polite"><span className="artifact-job-progress-track"><span/></span><p>{job.state === "running" ? "The Spark is producing artifacts." : job.state === "queued" ? "Waiting for the assigned Spark." : "Preparing the immutable job request."}</p></div>}
    {job.status_reason && <p className="artifact-job-reason" role={job.state === "failed" ? "alert" : undefined}>{job.status_reason}</p>}
    {job.result_evidence && <dl className="artifact-job-evidence">
      {job.result_evidence.elapsed_milliseconds != null && <><dt>Elapsed</dt><dd>{(job.result_evidence.elapsed_milliseconds / 1000).toLocaleString()} s</dd></>}
      {job.result_evidence.peak_memory_bytes != null && <><dt>Peak memory</dt><dd>{formatBytes(job.result_evidence.peak_memory_bytes)}</dd></>}
    </dl>}
    {job.state === "succeeded" && job.output_files.length === 0 && <p className="artifact-job-reason">This job succeeded without downloadable outputs.</p>}
    {job.state === "succeeded" && job.output_files.length > 0 && <ul className="artifact-output-list" aria-label={`${job.output_files.length} generated outputs`}>{job.output_files.map(file => <OutputPreview api={api} file={file} job={job} key={`${file.name}:${file.sha256}`}/>)}</ul>}
    {job.state === "cancelling" && <p className="artifact-job-notice" role="status">Cancellation was accepted and is waiting for the Controller to report a settled result.</p>}
    <div className="artifact-job-row-actions">
      {active && job.state !== "cancelling" && cancelCandidate !== job.id && <button type="button" className="button secondary" disabled={busyJobId === job.id} onClick={() => onCancel(job.id)}>Cancel job</button>}
      {active && job.state !== "cancelling" && cancelCandidate === job.id && <><p>Cancel this job and keep its audit history?</p><button type="button" className="danger" disabled={busyJobId === job.id} onClick={() => onConfirmCancel(job)}>{busyJobId === job.id ? "Cancelling…" : "Confirm cancel"}</button><button type="button" className="button secondary" onClick={() => onCancel(undefined)}>Keep running</button></>}
      {(job.state === "failed" || job.state === "cancelled") && <button type="button" className="button secondary" onClick={() => onPrepareRetry(job)}>Prepare retry</button>}
    </div>
    <TechnicalDetails compact items={[
      {label: "Job ID", value: job.id},
      {label: "Run ID", value: job.run_id},
      {label: "Contract SHA-256", value: job.contract_sha256},
      {label: "Input manifest", value: job.input_manifest_sha256},
      {label: "Output manifest", value: job.output_manifest_sha256 ?? ""},
    ]}/>
  </article>;
}

export function ArtifactJobWorkspace({api, detail, onBusyChange}: {api: LibraryApi; detail: LibraryViewRecipeDetail; onBusyChange?(busy: boolean): void}) {
  type ActiveRun = {run_id: string; state: string};
  const document = detail.definition;
  const jobInterfaces = useMemo(() => document.interfaces.filter((item): item is JobRecipeInterface => item.adapter !== "openai"), [document]);
  const [interfaceIndex, setInterfaceIndex] = useState(0);
  const jobInterface = jobInterfaces[interfaceIndex];
  const activeRun = detail.operational_state.runs.find((run): run is ActiveRun => typeof run === "object" && run !== null && "run_id" in run && typeof run.run_id === "string" && "state" in run && run.state === "running");
  const parameters = useMemo(() => recipeParameters(document), [document]);
  const input = jobInterface?.input ?? null;
  const inputSlots = useMemo<RecipeInputSlot[]>(() => {
    if (!input) return [];
    if (input.slots?.length) return input.slots;
    return [{
      id: "input",
      label: input.required ? "Source files" : "Optional source files",
      description: "Inputs accepted by this recipe revision.",
      media_types: input.media_types,
      extensions: [],
      min_files: input.required ? 1 : 0,
      max_files: 32,
      max_file_bytes: Math.min(input.max_bytes, CONTROLLER_FILE_LIMIT),
      max_total_bytes: Math.min(input.max_bytes * 32, CONTROLLER_TOTAL_LIMIT),
    }];
  }, [input]);
  const textSlot = inputSlots.find(slot => slot.media_types.includes("text/plain"));
  const [values, setValues] = useState<Record<string, Scalar>>(() => parameterDefaults(parameters));
  const [prompt, setPrompt] = useState("");
  const [filesBySlot, setFilesBySlot] = useState<Record<string, File[]>>({});
  const [timeoutSeconds, setTimeoutSeconds] = useState(3600);
  const [jobs, setJobs] = useState<ArtifactJob[]>([]);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobsError, setJobsError] = useState("");
  const [capabilities, setCapabilities] = useState<ArtifactJobCapabilities>();
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
  const [capabilitiesError, setCapabilitiesError] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [phase, setPhase] = useState("");
  const [transfer, setTransfer] = useState<{loaded: number; total: number}>();
  const [busyJobId, setBusyJobId] = useState<string>();
  const [cancelCandidate, setCancelCandidate] = useState<string>();
  const [retryNotice, setRetryNotice] = useState("");
  const [cancellationNotice, setCancellationNotice] = useState("");
  const [cancelRecovery, setCancelRecovery] = useState<CancelRecovery>();
  const [compactMobile, setCompactMobile] = useState(() => typeof window !== "undefined" && window.matchMedia?.("(max-width: 520px)").matches === true);
  const [expandedArchiveIds, setExpandedArchiveIds] = useState<Set<string>>(() => new Set());
  const heading = useRef<HTMLHeadingElement>(null);
  const submissionController = useRef<AbortController | undefined>(undefined);
  const createIntent = useRef<{fingerprint: string; requestId: string} | undefined>(undefined);
  const submitIntents = useRef(new Map<string, string>());
  const cancelIntents = useRef(new Map<string, string>());

  useEffect(() => {
    if (interfaceIndex >= jobInterfaces.length) setInterfaceIndex(0);
  }, [interfaceIndex, jobInterfaces.length]);
  useEffect(() => {
    setValues(parameterDefaults(parameters));
    setPrompt("");
    setFilesBySlot({});
    setSubmitError("");
    setRetryNotice("");
    setCancellationNotice("");
  }, [jobInterface, parameters]);
  const loadCapabilities = useCallback(async (signal?: AbortSignal) => {
    setCapabilitiesLoading(true);
    try {
      const value = await api.artifactJobCapabilities(signal);
      if (!signal?.aborted) { setCapabilities(value); setCapabilitiesError(""); }
    } catch (value) {
      if (!signal?.aborted) { setCapabilities(undefined); setCapabilitiesError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load artifact storage limits"); }
    } finally {
      if (!signal?.aborted) setCapabilitiesLoading(false);
    }
  }, [api]);
  useEffect(() => {
    if (!jobInterface) return;
    const controller = new AbortController();
    void loadCapabilities(controller.signal);
    return () => controller.abort();
  }, [jobInterface, loadCapabilities]);
  useEffect(() => () => submissionController.current?.abort(), []);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const media = window.matchMedia("(max-width: 520px)");
    const update = () => setCompactMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const loadJobs = useCallback(async (signal?: AbortSignal, announce = false) => {
    if (!activeRun) return;
    if (announce) setJobsLoading(true);
    try {
      const value = await api.artifactJobsForRun(activeRun.run_id, signal);
      if (!signal?.aborted) { setJobs(value.jobs); setJobsError(""); }
    } catch (value) {
      if (!signal?.aborted) setJobsError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load artifact jobs");
    } finally {
      if (!signal?.aborted && announce) setJobsLoading(false);
    }
  }, [activeRun, api]);

  useEffect(() => {
    if (!jobInterface || !activeRun) return;
    const controller = new AbortController();
    void loadJobs(controller.signal, true);
    return () => controller.abort();
  }, [activeRun, jobInterface, loadJobs]);

  useEffect(() => {
    if (!activeRun || !jobs.some(job => !TERMINAL_STATES.has(job.state))) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void loadJobs(controller.signal), 2000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [activeRun, jobs, loadJobs]);

  if (!jobInterface) return null;
  const adapter = jobInterface.adapter;

  const allowsPromptFile = textSlot !== undefined;
  const promptLimit = Math.min(textSlot?.max_file_bytes ?? CONTROLLER_FILE_LIMIT, CONTROLLER_FILE_LIMIT);
  const selectedFiles = inputSlots.flatMap(slot => (filesBySlot[slot.id] ?? []).map(file => ({slot, file})));
  const parameterErrors = parameters.flatMap(parameter => {
    const error = parameterError(parameter, values[parameter.name] ?? parameter.default);
    return error ? [{name: parameter.name, error}] : [];
  });
  const promptBytes = new TextEncoder().encode(prompt.trim()).byteLength;
  const fileBytes = selectedFiles.reduce((total, item) => total + item.file.size, 0);
  const inputCount = selectedFiles.length + (allowsPromptFile && prompt.trim() ? 1 : 0);
  const inputErrors: string[] = [];
  for (const slot of inputSlots) {
    const slotFiles = filesBySlot[slot.id] ?? [];
    const promptCount = slot.id === textSlot?.id && prompt.trim() ? 1 : 0;
    const count = slotFiles.length + promptCount;
    const total = slotFiles.reduce((sum, file) => sum + file.size, 0) + (promptCount ? promptBytes : 0);
    if (count < slot.min_files) inputErrors.push(`${slot.label}: add at least ${slot.min_files} file${slot.min_files === 1 ? "" : "s"}.`);
    if (count > slot.max_files) inputErrors.push(`${slot.label}: use no more than ${slot.max_files} file${slot.max_files === 1 ? "" : "s"}.`);
    if (slotFiles.some(file => file.size > Math.min(slot.max_file_bytes, CONTROLLER_FILE_LIMIT))) inputErrors.push(`${slot.label}: every file must be ${formatBytes(Math.min(slot.max_file_bytes, CONTROLLER_FILE_LIMIT))} or smaller.`);
    if (slotFiles.some(file => !slot.media_types.includes(fileMediaType(slot, file)))) inputErrors.push(`${slot.label}: every file must use an allowed media type.`);
    if (slot.extensions.length > 0 && slotFiles.some(file => !slot.extensions.some(extension => file.name.toLowerCase().endsWith(extension.toLowerCase())))) inputErrors.push(`${slot.label}: every filename must end in ${slot.extensions.join(" or ")}.`);
    if (total > Math.min(slot.max_total_bytes, CONTROLLER_TOTAL_LIMIT)) inputErrors.push(`${slot.label}: combined files must be ${formatBytes(Math.min(slot.max_total_bytes, CONTROLLER_TOTAL_LIMIT))} or smaller.`);
  }
  if (inputCount > (capabilities?.transport.max_input_files ?? 32)) inputErrors.push(`Use no more than ${capabilities?.transport.max_input_files ?? 32} inputs across all slots.`);
  if (promptBytes > promptLimit) inputErrors.push(`Prompt exceeds the ${formatBytes(promptLimit)} input limit.`);
  const controllerTotalLimit = capabilities?.transport.max_input_total_bytes ?? CONTROLLER_TOTAL_LIMIT;
  if (promptBytes + fileBytes > controllerTotalLimit) inputErrors.push(`Combined inputs must be ${formatBytes(controllerTotalLimit)} or smaller.`);
  if (capabilities && promptBytes + fileBytes > capabilities.storage.remaining_bytes) inputErrors.push(`Artifact storage has only ${formatBytes(capabilities.storage.remaining_bytes)} remaining.`);
  const normalizedNames = selectedFiles.map(item => filename(item.file.name)).concat(textSlot && prompt.trim() ? ["prompt.txt"] : []);
  if (new Set(normalizedNames).size !== normalizedNames.length) inputErrors.push("Input filenames must be unique after removing unsupported characters.");
  if (normalizedNames.some(name => capabilities?.transport.reserved_input_names.includes(name))) inputErrors.push("One selected filename is reserved by the controller.");
  const maximumTimeout = capabilities?.transport.max_timeout_seconds ?? 3600;
  if (timeoutSeconds < 1 || timeoutSeconds > maximumTimeout) inputErrors.push(`Timeout must be between 1 and ${maximumTimeout.toLocaleString()} seconds.`);
  const output = jobInterface.output;
  const outputSlots = output?.slots ?? [];
  const outputContractReady = output?.path === "/outputs" && outputSlots.length > 0 && typeof output.max_total_bytes === "number";
  if (!outputContractReady) inputErrors.push("This recipe revision has no complete artifact output contract.");
  const preflightErrors = [...parameterErrors.map(item => `${item.name}: ${item.error}`), ...inputErrors];
  const canSubmit = Boolean(activeRun && capabilities) && !phase && preflightErrors.length === 0
    && cancelRecovery?.source !== "upload";
  const exactOutputMedia = [...new Set(outputSlots.flatMap(slot => slot.media_types))];
  const outputLimits = {
    max_files: Math.min(outputSlots.reduce((total, slot) => total + slot.max_files, 0) || 1, capabilities?.transport.max_output_files ?? 32),
    max_file_bytes: Math.min(Math.max(...outputSlots.map(slot => slot.max_file_bytes), 1), capabilities?.transport.max_output_file_bytes ?? 1024 ** 3),
    max_total_bytes: Math.min(output?.max_total_bytes ?? 1, capabilities?.transport.max_output_total_bytes ?? 2 * 1024 ** 3),
    allowed_media_types: exactOutputMedia.length > 0 ? exactOutputMedia : outputMedia[adapter],
  };
  const featuredTerminalId = jobs.find(job => TERMINAL_STATES.has(job.state))?.id;

  async function payloads(signal: AbortSignal): Promise<InputPayload[]> {
    const sources: Array<{slot: string; name: string; media_type: string; blob: Blob}> = selectedFiles.map(({slot, file}) => ({slot: slot.id, name: filename(file.name), media_type: fileMediaType(slot, file), blob: file}));
    if (textSlot && prompt.trim()) sources.push({slot: textSlot.id, name: "prompt.txt", media_type: "text/plain", blob: new Blob([prompt.trim()], {type: "text/plain"})});
    if (new Set(sources.map(source => source.name)).size !== sources.length) throw new Error("Input filenames must be unique after removing unsupported characters.");
    const total = sources.reduce((sum, source) => sum + source.blob.size, 0);
    let completed = 0;
    const prepared: InputPayload[] = [];
    for (const source of sources) {
      const sha256 = await hashArtifactBlob(source.blob, {signal, onProgress: progress => setTransfer({loaded: completed + progress.loaded, total})});
      prepared.push({declaration: {slot: source.slot, name: source.name, media_type: source.media_type, size_bytes: source.blob.size, sha256}, blob: source.blob});
      completed += source.blob.size;
    }
    return prepared;
  }

  function createRequestId(runId: string, body: ArtifactJobCreateInput): string {
    const fingerprint = JSON.stringify({runId, body});
    if (!createIntent.current || createIntent.current.fingerprint !== fingerprint) {
      createIntent.current = {fingerprint, requestId: crypto.randomUUID()};
    }
    return createIntent.current.requestId;
  }

  function submitRequestId(job: ArtifactJob): string {
    const existing = submitIntents.current.get(job.id);
    if (job.submit_request_id) {
      if (existing && existing !== job.submit_request_id) {
        throw new Error(`Job ${job.id} carries a submit receipt for a different request identity`);
      }
      if (!existing && job.operation_id) {
        throw new Error(`Job ${job.id} is already submitted, but this page has no matching submit intent to recover`);
      }
      return job.submit_request_id;
    }
    if (existing) return existing;
    const requestId = crypto.randomUUID();
    submitIntents.current.set(job.id, requestId);
    return requestId;
  }

  function cancelRequestId(job: ArtifactJob, reason: string): string {
    const receipt = cancelReceipt(job);
    if (receipt?.reason === reason) return receipt.requestId;
    const identity = `${job.id}\n${reason}`;
    const existing = cancelIntents.current.get(identity);
    if (existing) return existing;
    const requestId = crypto.randomUUID();
    cancelIntents.current.set(identity, requestId);
    return requestId;
  }

  async function createWithRecovery(
    runId: string,
    body: ArtifactJobCreateInput,
    requestId: string,
    signal: AbortSignal,
  ): Promise<ArtifactJob> {
    let created: ArtifactJob | undefined;
    try {
      created = await api.createArtifactJob(runId, body, requestId, signal);
    } catch (error) {
      if (!mayHaveBeenAccepted(error)) throw error;
      let existing: ArtifactJob | undefined;
      try {
        existing = await api.artifactJobByRequestId(requestId);
      } catch (lookupError) {
        if (responseStatus(lookupError) !== 404) {
          if (isDefinitiveHttpRefusal(lookupError)) throw lookupError;
          throw new Error(`Create outcome for request ${requestId} is unknown: ${errorText(lookupError, "the request lookup failed")}`);
        }
      }
      if (existing) assertJobIdentity(existing, undefined, runId, "Create request lookup");
      if (signal.aborted || isAbort(error)) {
        if (existing) return existing;
        throw abortError();
      }
      // Replaying the exact body with the original key asks the owner to verify
      // the stored intent as well as returning its durable draft identity.
      try {
        created = await api.createArtifactJob(runId, body, requestId);
      } catch (replayError) {
        if (isDefinitiveHttpRefusal(replayError)) throw replayError;
        throw new Error(`Create request ${requestId} could not be recovered: ${errorText(replayError, "the exact request replay failed")}`);
      }
    }
    if (!created) throw new Error(`Create request ${requestId} returned no artifact job`);
    assertJobIdentity(created, undefined, runId, "Create response");
    return created;
  }

  async function readJobForRecovery(jobId: string, runId: string, action: string): Promise<ArtifactJob> {
    let observed: ArtifactJob;
    try {
      observed = await api.artifactJob(jobId);
    } catch (error) {
      if (isDefinitiveHttpRefusal(error)) throw error;
      throw new Error(`${action} outcome for job ${jobId} is unknown: ${errorText(error, "the job receipt lookup failed")}`);
    }
    assertJobIdentity(observed, jobId, runId, `${action} receipt lookup`);
    return observed;
  }

  async function submitWithRecovery(
    job: ArtifactJob,
    expectedRunId: string,
    requestId: string,
    signal: AbortSignal,
  ): Promise<ArtifactJob> {
    try {
      const submitted = await api.submitArtifactJob(job.id, requestId, signal);
      assertJobIdentity(submitted, job.id, expectedRunId, "Submit response");
      if (!submitted.operation_id || submitted.submit_request_id !== requestId) {
        throw new Error("Submit response does not carry this request's operation receipt");
      }
      return submitted;
    } catch (error) {
      if (!mayHaveBeenAccepted(error)) throw error;
      const observed = await readJobForRecovery(job.id, expectedRunId, "Submit");
      if (observed.operation_id) {
        if (observed.submit_request_id === requestId) return observed;
        throw new Error(`Job ${job.id} was submitted under a different request identity`);
      }
      if (observed.state !== "ready") throw error;
      if (signal.aborted || isAbort(error)) throw abortError();
      try {
        const replayed = await api.submitArtifactJob(job.id, requestId, signal);
        assertJobIdentity(replayed, job.id, expectedRunId, "Submit replay");
        if (!replayed.operation_id || replayed.submit_request_id !== requestId) {
          throw new Error("Submit replay does not carry this request's operation receipt");
        }
        return replayed;
      } catch (replayError) {
        if (!mayHaveBeenAccepted(replayError)) throw replayError;
        const latest = await readJobForRecovery(job.id, expectedRunId, "Submit");
        if (latest.operation_id && latest.submit_request_id === requestId) return latest;
        if (latest.operation_id) throw new Error(`Job ${job.id} was submitted under a different request identity`);
        if (signal.aborted || isAbort(replayError)) throw abortError();
        throw new Error(`Submit request for job ${job.id} could not be recovered: ${errorText(replayError, "the exact request replay failed")}`);
      }
    }
  }

  async function cancelWithRecovery(
    jobId: string,
    expectedRunId: string,
    reason: string,
    requestId: string,
  ): Promise<ArtifactJob> {
    try {
      const cancelled = await api.cancelArtifactJob(jobId, reason, requestId);
      assertJobIdentity(cancelled, jobId, expectedRunId, "Cancel response");
      if (!isCancelReceipt(cancelled, requestId, reason)) {
        throw new Error("Cancel response does not carry this request's cancellation receipt");
      }
      return cancelled;
    } catch (error) {
      if (!mayHaveBeenAccepted(error)) throw error;
      const observed = await readJobForRecovery(jobId, expectedRunId, "Cancellation");
      if (isCancelReceipt(observed, requestId, reason)) return observed;
      if (observed.state === "cancelling" || TERMINAL_STATES.has(observed.state)) {
        throw new Error(`Controller reports job ${jobId} as ${observed.state}, without this cancellation receipt`);
      }
      try {
        const replayed = await api.cancelArtifactJob(jobId, reason, requestId);
        assertJobIdentity(replayed, jobId, expectedRunId, "Cancel replay");
        if (isCancelReceipt(replayed, requestId, reason)) return replayed;
      } catch (replayError) {
        if (!mayHaveBeenAccepted(replayError)) throw replayError;
      }
      const latest = await readJobForRecovery(jobId, expectedRunId, "Cancellation");
      if (isCancelReceipt(latest, requestId, reason)) return latest;
      throw new Error(`Controller reports job ${jobId} as ${latest.state}, without this cancellation receipt`);
    }
  }

  async function completeCancellation(
    jobId: string,
    runId: string,
    reason: string,
    requestId: string,
    source: CancelRecovery["source"],
  ): Promise<void> {
    if (activeRun?.run_id !== runId) {
      setCancelRecovery({jobId, runId, reason, requestId, source, message: "The active recipe run changed; select the original run before retrying cancellation"});
      return;
    }
    try {
      const updated = await cancelWithRecovery(jobId, runId, reason, requestId);
      setJobs(current => current.map(item => item.id === updated.id ? updated : item));
      setCancelRecovery(undefined);
      setCancelCandidate(undefined);
      cancelIntents.current.delete(`${jobId}\n${reason}`);
      if (source === "upload") {
        createIntent.current = undefined;
        setSubmitError("");
        setCancellationNotice(updated.state === "cancelling"
          ? `Local upload stopped. Cancellation is pending for draft ${updated.id}.`
          : `Local upload stopped. Controller cancelled draft ${updated.id}.`);
      }
    } catch (value) {
      if (source === "upload") setSubmitError("");
      setCancelRecovery({
        jobId,
        runId,
        reason,
        requestId,
        source,
        message: errorText(value, "The Controller did not confirm cancellation"),
      });
    }
  }

  async function submit() {
    if (!activeRun || !canSubmit) return;
    setSubmitError("");
    setRetryNotice("");
    const controller = new AbortController();
    submissionController.current = controller;
    let createdJob: ArtifactJob | undefined;
    onBusyChange?.(true);
    try {
      setPhase("Checking input digests…");
      const prepared = await payloads(controller.signal);
      const createParameters: ArtifactJobCreateInput["parameters"] = {};
      for (const parameter of parameters) {
        const value = values[parameter.name] ?? parameter.default;
        createParameters[parameter.name] = value;
      }
      const body: ArtifactJobCreateInput = {
        interface: adapter,
        parameters: createParameters,
        inputs: prepared.map(item => item.declaration),
        output_limits: outputLimits,
        timeout_seconds: timeoutSeconds,
      };
      const createKey = createRequestId(activeRun.run_id, body);
      setPhase("Creating durable job…");
      let job = await createWithRecovery(activeRun.run_id, body, createKey, controller.signal);
      createdJob = job;
      if (controller.signal.aborted) throw abortError();
      const uploadedIndexes = recoveredInputIndexes(job, prepared);
      if (job.operation_id) {
        const submitIntent = submitIntents.current.get(job.id);
        if (!submitIntent || job.submit_request_id !== submitIntent) {
          throw new Error(`Job ${job.id} is already submitted under a request identity this page cannot verify`);
        }
      } else if (job.state !== "draft" && job.state !== "ready") {
        throw new Error(`Job ${job.id} is ${job.state}; its inputs cannot be resumed safely`);
      }
      setJobs(current => [job, ...current.filter(item => item.id !== job.id)]);
      const uploadTotal = prepared.reduce((total, current) => total + current.blob.size, 0);
      let completed = prepared.reduce((total, item, index) => total + (uploadedIndexes.has(index) ? item.blob.size : 0), 0);
      setTransfer({loaded: completed, total: uploadTotal});
      for (const [index, item] of prepared.entries()) {
        if (uploadedIndexes.has(index)) continue;
        if (job.state !== "draft") {
          throw new Error(`Job ${job.id} is ${job.state} but is missing a declared input; it cannot be resumed safely`);
        }
        setPhase(`Uploading input ${index + 1} of ${prepared.length}…`);
        const alreadyUploaded = completed;
        const uploadJobId = job.id;
        const uploadedJob = await api.uploadArtifactJobInput(uploadJobId, item.declaration, item.blob, controller.signal, progress => setTransfer({loaded: alreadyUploaded + progress.loaded, total: uploadTotal}));
        assertJobIdentity(uploadedJob, uploadJobId, activeRun.run_id, "Input upload");
        job = uploadedJob;
        if (controller.signal.aborted) throw abortError();
        completed += item.blob.size;
        if (!recoveredInputIndexes(job, prepared).has(index)) {
          throw new Error(`Controller did not confirm that input ${item.declaration.name} was attached to job ${job.id}`);
        }
        uploadedIndexes.add(index);
        setTransfer({loaded: completed, total: uploadTotal});
        setJobs(current => current.map(existing => existing.id === job.id ? job : existing));
      }
      if (job.state === "draft") {
        setPhase("Finalizing immutable inputs…");
        const finalizeJobId = job.id;
        const finalizedJob = await api.finalizeArtifactJob(finalizeJobId, controller.signal);
        assertJobIdentity(finalizedJob, finalizeJobId, activeRun.run_id, "Finalize response");
        job = finalizedJob;
        if (controller.signal.aborted) throw abortError();
        if (job.state !== "ready") throw new Error(`Controller did not confirm that job ${job.id} is ready for submission`);
      }
      setPhase("Submitting to the Spark…");
      const submitKey = submitRequestId(job);
      job = await submitWithRecovery(job, activeRun.run_id, submitKey, controller.signal);
      if (controller.signal.aborted) throw abortError();
      submitIntents.current.delete(job.id);
      if (createIntent.current?.requestId === createKey) createIntent.current = undefined;
      setJobs(current => [job, ...current.filter(existing => existing.id !== job.id)]);
      setFilesBySlot({});
      setPhase("");
      setTransfer(undefined);
      setCancellationNotice("");
      queueMicrotask(() => heading.current?.focus());
    } catch (value) {
      setPhase("");
      setTransfer(undefined);
      if (isAbort(value)) {
        setFilesBySlot({});
        if (createdJob) {
          const reason = "Cancelled by operator during browser transfer";
          const requestId = cancelRequestId(createdJob, reason);
          setSubmitError(`Local transfer stopped. Checking cancellation of durable draft ${createdJob.id}…`);
          void completeCancellation(createdJob.id, activeRun.run_id, reason, requestId, "upload");
        } else {
          setSubmitError("Input preparation was cancelled before a durable artifact job was created.");
        }
      } else setSubmitError(value instanceof Error ? value.message.slice(0, 256) : "Unable to submit artifact job");
      void loadJobs(undefined);
    } finally {
      onBusyChange?.(false);
      if (submissionController.current === controller) submissionController.current = undefined;
    }
  }

  async function confirmCancel(job: ArtifactJob) {
    setBusyJobId(job.id);
    const reason = "Cancelled by operator from Library";
    const runId = activeRun?.run_id;
    if (!runId || job.run_id !== runId) {
      setCancelRecovery({
        jobId: job.id,
        runId: job.run_id,
        reason,
        requestId: cancelRequestId(job, reason),
        source: "history",
        message: "This job does not belong to the active recipe run",
      });
      setBusyJobId(undefined);
      return;
    }
    await completeCancellation(job.id, runId, reason, cancelRequestId(job, reason), "history");
    setBusyJobId(undefined);
  }

  async function retryCancellation() {
    if (!cancelRecovery) return;
    setBusyJobId(cancelRecovery.jobId);
    await completeCancellation(
      cancelRecovery.jobId,
      cancelRecovery.runId,
      cancelRecovery.reason,
      cancelRecovery.requestId,
      cancelRecovery.source,
    );
    setBusyJobId(undefined);
  }

  function prepareRetry(job: ArtifactJob) {
    setFilesBySlot({});
    setSubmitError("");
    setRetryNotice(`Retry prepared from ${formatDate(job.created_at)}. Review parameters and reselect local inputs; browsers do not retain file access after submission.`);
    heading.current?.scrollIntoView?.({block: "start"});
    queueMicrotask(() => heading.current?.focus());
  }

  return <section className="artifact-job-workspace" aria-labelledby="artifact-job-heading">
    <header className="artifact-job-heading"><div><h4 id="artifact-job-heading" ref={heading} tabIndex={-1}>Create artifacts</h4><p>Send a bounded, durable {humanizeIdentifier(adapter)} request to this running recipe.</p></div><StatusPill tone={activeRun ? "healthy" : "warning"}>{activeRun ? "Run ready for jobs" : "Start this recipe first"}</StatusPill></header>
    {cancellationNotice && <p className="artifact-job-notice" role="status">{cancellationNotice}</p>}
    {!activeRun && <div className="artifact-job-empty"><strong>No running recipe</strong><p>Install and load this recipe before submitting an artifact job. The form remains visible so you can inspect its exact contract.</p></div>}
    <div className="artifact-job-layout">
      <form className="artifact-job-form" onSubmit={event => { event.preventDefault(); void submit(); }} noValidate>
        {jobInterfaces.length > 1 && <label htmlFor="artifact-job-interface"><span>Job interface</span><select id="artifact-job-interface" aria-label="Job interface" value={interfaceIndex} onChange={event => setInterfaceIndex(Number(event.target.value))}>{jobInterfaces.map((item, index) => <option value={index} key={`${item.adapter}:${item.path ?? ""}:${index}`}>{humanizeIdentifier(item.adapter)} · {item.input?.slots?.length ?? 0} bounded slot{item.input?.slots?.length === 1 ? "" : "s"}</option>)}</select><small>Each declared interface keeps its own exact input and output boundary. Changing interface clears local, unsubmitted inputs.</small></label>}
        {textSlot && <label htmlFor="artifact-job-prompt"><span>{textSlot.label}{textSlot.min_files > 0 ? "" : " (optional)"}</span><textarea id="artifact-job-prompt" rows={5} value={prompt} onChange={event => setPrompt(event.target.value)} aria-label={textSlot.label} aria-required={textSlot.min_files ? "true" : undefined} aria-invalid={inputErrors.some(error => error.startsWith(`${textSlot.label}:`)) || undefined} aria-describedby="artifact-job-prompt-help" placeholder="Describe the artifact to produce"/><small id="artifact-job-prompt-help">{textSlot.description} Saved as UTF-8 <code>prompt.txt</code> · {formatBytes(promptBytes)} of {formatBytes(promptLimit)}</small></label>}
        {!textSlot && <div className="artifact-job-contract-notice"><strong>No prompt control declared</strong><p>This recipe revision does not authorize a prompt file. Add its required source files below, or update the recipe contract before expecting text-guided output.</p></div>}
        {parameters.length > 0 && <fieldset className="artifact-job-parameters"><legend>Recipe settings</legend>{parameters.map(parameter => {
          const value = values[parameter.name] ?? parameter.default;
          const error = parameterErrors.find(item => item.name === parameter.name)?.error;
          const describedBy = `${parameter.name}-help${error ? ` ${parameter.name}-error` : ""}`;
          if (parameter.type === "boolean") return <label className="artifact-job-check" key={parameter.name}><input type="checkbox" checked={Boolean(value)} onChange={event => setValues(current => ({...current, [parameter.name]: event.target.checked}))}/><span><strong>{humanizeIdentifier(parameter.name)}</strong><small id={`${parameter.name}-help`}>{parameter.description}</small></span></label>;
          return <label key={parameter.name} htmlFor={`artifact-job-${parameter.name}`}><span>{humanizeIdentifier(parameter.name)}</span><input id={`artifact-job-${parameter.name}`} type={parameter.type === "integer" ? "number" : "text"} value={String(value)} aria-label={humanizeIdentifier(parameter.name)} aria-invalid={Boolean(error)} aria-describedby={describedBy} onChange={event => setValues(current => ({...current, [parameter.name]: parameter.type === "integer" ? Number(event.target.value) : event.target.value}))}/><small id={`${parameter.name}-help`}>{parameter.description}</small>{error && <small className="artifact-job-field-error" id={`${parameter.name}-error`}>{error}</small>}</label>;
        })}</fieldset>}
        {inputSlots.map(slot => {
          const fileMedia = slot.media_types.filter(mediaType => mediaType !== "text/plain");
          if (fileMedia.length === 0) return null;
          const id = `artifact-job-files-${slot.id}`;
          const fieldErrors = inputErrors.filter(error => error.startsWith(`${slot.label}:`));
          const helpId = `${id}-help`;
          const errorId = `${id}-error`;
          return <label htmlFor={id} key={slot.id}><span>{slot.label}{slot.min_files > 0 && slot.id !== textSlot?.id ? "" : " (optional)"}</span><input id={id} type="file" multiple={slot.max_files > 1} required={slot.min_files > 0 && slot.id !== textSlot?.id} accept={[...fileMedia, ...slot.extensions].join(",")} aria-label={slot.label} aria-invalid={fieldErrors.length > 0 || undefined} aria-describedby={`${helpId}${fieldErrors.length ? ` ${errorId}` : ""}`} onChange={event => setFilesBySlot(current => ({...current, [slot.id]: Array.from(event.target.files ?? [])}))}/><small id={helpId}>{slot.description} {fileMedia.join(" · ")} · {slot.min_files}–{slot.max_files} files · {formatBytes(Math.min(slot.max_file_bytes, CONTROLLER_FILE_LIMIT))} each · {formatBytes(Math.min(slot.max_total_bytes, CONTROLLER_TOTAL_LIMIT))} total</small>{fieldErrors.length > 0 && <small className="artifact-job-field-error" id={errorId}>{fieldErrors.join(" ")}</small>}</label>;
        })}
        {selectedFiles.length > 0 && <ul className="artifact-input-list" aria-label="Selected input files">{selectedFiles.map(({slot, file}) => <li key={`${slot.id}:${file.name}:${file.lastModified}`}><span>{file.name}</span><small>{slot.label} · {file.type || "Unknown media type"} · {formatBytes(file.size)}</small></li>)}</ul>}
        <label htmlFor="artifact-job-timeout"><span>Maximum run time</span><input id="artifact-job-timeout" type="number" min={1} max={maximumTimeout} value={timeoutSeconds} aria-label="Maximum run time" onChange={event => setTimeoutSeconds(Number(event.target.value))}/><small>Seconds · the controller stops this job after at most {maximumTimeout.toLocaleString()} seconds.</small></label>
        {capabilitiesError && <div className="artifact-job-error" role="alert"><strong>Storage preflight unavailable</strong><p>{capabilitiesError}</p><p>Submission stays disabled until the controller can report its current limits.</p><button type="button" className="button secondary" disabled={capabilitiesLoading} onClick={() => void loadCapabilities()}>{capabilitiesLoading ? "Retrying preflight…" : "Retry storage preflight"}</button></div>}
        <section className={`artifact-job-preflight${preflightErrors.length ? " has-errors" : ""}`} aria-label="Job preflight">
          <div><strong>{preflightErrors.length ? "Resolve preflight checks" : "Ready to submit"}</strong><span>{inputCount} input{inputCount === 1 ? "" : "s"} · {formatBytes(promptBytes + fileBytes)} · up to {outputLimits.max_files} outputs</span></div>
          {capabilities && <p className="artifact-storage-capacity">Controller storage: {formatBytes(capabilities.storage.remaining_bytes)} free of {formatBytes(capabilities.storage.max_stored_bytes)}</p>}
          {preflightErrors.length > 0 && <ul>{preflightErrors.map(error => <li key={error}>{error}</li>)}</ul>}
          <details><summary>Output boundary</summary><p>{formatBytes(outputLimits.max_total_bytes)} total · {formatBytes(outputLimits.max_file_bytes)} per file · {outputLimits.allowed_media_types.join(" · ")}</p></details>
        </section>
        {retryNotice && <p className="artifact-job-notice" role="status">{retryNotice}</p>}
        {submitError && <div className="artifact-job-error" role="alert"><strong>Submission needs recovery</strong><p>{submitError}</p><button type="button" className="button secondary" onClick={() => setSubmitError("")}>Review and try again</button></div>}
        {cancelRecovery && <div className="artifact-job-error" role="alert"><strong>Cancellation needs recovery</strong><p>Job {cancelRecovery.jobId}, request {cancelRecovery.requestId}: {cancelRecovery.message}</p><button type="button" className="button secondary" disabled={busyJobId === cancelRecovery.jobId} onClick={() => void retryCancellation()}>{busyJobId === cancelRecovery.jobId ? "Retrying cancellation…" : "Retry cancellation"}</button></div>}
        {phase && <div className="artifact-transfer" role="status" aria-live="polite"><div><strong>{phase}</strong><span>{transfer ? `${formatBytes(transfer.loaded)} of ${formatBytes(transfer.total)}` : "Preparing…"}</span></div>{transfer && <progress value={transfer.loaded} max={Math.max(transfer.total, 1)}/>}<button type="button" className="button secondary" onClick={() => submissionController.current?.abort()}>Cancel transfer</button></div>}
        <button type="submit" disabled={!canSubmit}>{capabilities ? "Submit artifact job" : capabilitiesError ? "Storage preflight required" : "Checking controller capacity…"}</button>
      </form>
      <section className="artifact-job-history" aria-label="Artifact job history">
        <header><h5>Recent jobs</h5><button type="button" className="button secondary" disabled={jobsLoading} onClick={() => void loadJobs(undefined, true)}>{jobsLoading ? "Refreshing…" : "Refresh"}</button></header>
        {jobsLoading && jobs.length === 0 && <div className="artifact-job-history-empty" role="status"><span className="loading-orb" aria-hidden="true"/><p>Loading durable job history…</p></div>}
        {jobsError && <div className="artifact-job-error" role="alert"><strong>Job history unavailable</strong><p>{jobsError}</p><button type="button" className="button secondary" onClick={() => void loadJobs(undefined, true)}>Retry history</button></div>}
        {!jobsLoading && !jobsError && jobs.length === 0 && <div className="artifact-job-history-empty"><strong>No artifact jobs yet</strong><p>Your first submitted job will stay here across refreshes, including its progress and outputs.</p></div>}
        {jobs.map(job => {
          const history = <JobHistory api={api} busyJobId={busyJobId} cancelCandidate={cancelCandidate} job={job} onCancel={setCancelCandidate} onConfirmCancel={value => void confirmCancel(value)} onPrepareRetry={prepareRetry}/>;
          if (!TERMINAL_STATES.has(job.state) || job.id === featuredTerminalId) return <div className="artifact-job-featured" key={job.id}>{history}</div>;
          const archiveOpen = !compactMobile || expandedArchiveIds.has(job.id);
          return <details className="artifact-job-archive" key={job.id} open={archiveOpen} onToggle={event => {
            const open = event.currentTarget.open;
            if (!compactMobile || open === expandedArchiveIds.has(job.id)) return;
            setExpandedArchiveIds(current => {
              const next = new Set(current);
              open ? next.add(job.id) : next.delete(job.id);
              return next;
            });
          }}><summary><span><StatusPill tone={jobTone(job.state)}>{humanizeIdentifier(job.state)}</StatusPill><strong>{humanizeIdentifier(job.interface)}</strong></span><span>{formatDate(job.created_at)}<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m6 9 6 6 6-6" strokeLinecap="round" strokeLinejoin="round"/></svg></span></summary>{history}</details>;
        })}
      </section>
    </div>
  </section>;
}

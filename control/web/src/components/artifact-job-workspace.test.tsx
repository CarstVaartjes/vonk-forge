import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ArtifactJob, LibraryApi, LibraryRecipeDetail, RecipeDefinition} from "../api/types";
import {ArtifactJobWorkspace} from "./artifact-job-workspace";
import {hashArtifactBlob} from "./artifact-hash";

vi.mock("./artifact-hash", () => ({
  hashArtifactBlob: vi.fn(async (blob: Blob, options: {onProgress?(value: {loaded: number; total: number}): void}) => {
    options.onProgress?.({loaded: blob.size, total: blob.size});
    return "9".repeat(64);
  }),
}));

const run = {
  installation_id: "installation-chat",
  mapping_id: "mapping-chat",
  node_ids: ["node-alpha"],
  recipe_revision_id: "revision-chat",
  route_state: "published" as const,
  run_id: "00000000-0000-4000-8000-000000000010",
  state: "running" as const,
};

function detail(running = true): LibraryRecipeDetail {
  const definition = canonicalDefinition([canonicalImageInterface()]);
  return {
    schema_version: 2,
    generated_at: "2026-08-28T12:00:00Z",
    recipe: {recipe_id: "recipe-chat", publisher: "local", slug: "qwen-chat", title: "Qwen Chat", description: "Fast distributed chat model.", content_sha256: "a".repeat(64)},
    definition,
    topology: definition.topology,
    model_documents: [],
    model_capabilities: {schema_version: 2, state: "unknown", facts: [], provenance: null, reasons: []},
    recipe_capabilities: {schema_version: 2, state: "unknown", facts: [], provenance: null, reasons: []},
    operational_state: {builds: [], mappings: [], installations: [], runs: running ? [run] : []},
    placement: [],
    reasons: [],
  } as unknown as LibraryRecipeDetail;
}

function canonicalImageInterface() {
  return {
    adapter: "image-job" as const,
    path: "/outputs" as const,
    input: {path: "/inputs" as const, required: true, media_types: ["text/plain", "image/png"], max_bytes: 16_384, slots: [
      {id: "prompt", label: "Prompt", description: "A UTF-8 generation prompt.", media_types: ["text/plain"], extensions: [".txt"], min_files: 1, max_files: 1, max_file_bytes: 16_384, max_total_bytes: 16_384},
      {id: "reference", label: "Reference image", description: "An optional visual reference.", media_types: ["image/png"], extensions: [".png"], min_files: 0, max_files: 1, max_file_bytes: 8_388_608, max_total_bytes: 8_388_608},
    ]},
    output: {path: "/outputs" as const, max_total_bytes: 25_165_824, slots: [
      {id: "images", label: "Generated images", description: "Rendered image results.", media_types: ["image/png"], extensions: [".png"], min_files: 1, max_files: 2, max_file_bytes: 8_388_608, max_total_bytes: 16_777_216},
      {id: "metadata", label: "Metadata", description: "Generation metadata.", media_types: ["application/json"], extensions: [".json"], min_files: 0, max_files: 1, max_file_bytes: 8_388_608, max_total_bytes: 8_388_608},
    ]},
  };
}

function canonicalDefinition(interfaces: unknown[]): RecipeDefinition {
  return {
    schema_version: 2,
    kind: "recipe",
    identity: {publisher: "local", slug: "qwen-chat"},
    metadata: {title: "Qwen Chat", description: "Fast distributed chat model.", tags: ["chat"]},
    models: [],
    execution: {mode: "image", image: {repository: "example/qwen", digest: `sha256:${"b".repeat(64)}`, platform: "linux/arm64"}},
    runtime: {engine: "vllm", entrypoint: ["serve"], arguments: [], environment: [], lifecycle: {pre_start: [], post_stop: [], stop_timeout_seconds: 30}},
    settings: {kind: "job", knobs: {steps: {value: 24, change_effect: "restart"}, negative_prompt: {value: "", change_effect: "restart"}}},
    topology: {name: "solo", mode: "single", node_count: 1, parallelism: {world_size: 1, tensor: 1, pipeline: 1, data: 1, backend: "local"}, fabric: {connectivity: "none", minimum_bandwidth_mbps: 0}, roles: [{name: "entrypoint", count: 1, endpoint_owner: true, resources: {disk: {image_bytes: 1, artifact_bytes: 1, staging_bytes: 1, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 1}, memory: {kind: "host", startup_peak_bytes: 1, steady_state_bytes: 1, runtime_growth_bytes: 0, system_reserve_bytes: 1}}}], start_order: ["entrypoint"], stop_order: ["entrypoint"]},
    interfaces,
    validation: {benchmarks: [], serving: {interface: "image-job", checks: []}},
    provenance: {source_kind: "local", source_reference: null, attribution: []},
    release: {version: "3", released_at: "2026-08-28T12:00:00Z", history: []},
  } as unknown as RecipeDefinition;
}

function job(input: Partial<ArtifactJob> = {}): ArtifactJob {
  return {
    id: "00000000-0000-4000-8000-000000000020",
    run_id: run.run_id,
    operation_id: "operation-1",
    interface: "image-job",
    state: "succeeded",
    contract_sha256: "8".repeat(64),
    compiled_contract: {},
    input_manifest_sha256: "a".repeat(64),
    input_total_bytes: 12,
    input_declarations: [{slot: "prompt", name: "prompt.txt", media_type: "text/plain", size_bytes: 12, sha256: "b".repeat(64)}],
    input_files: [{slot: "prompt", name: "prompt.txt", media_type: "text/plain", size_bytes: 12, sha256: "b".repeat(64)}],
    output_limits: {max_files: 3, max_file_bytes: 8_388_608, max_total_bytes: 25_165_824, allowed_media_types: ["image/png", "application/json"]},
    output_manifest_sha256: "c".repeat(64),
    output_files: [],
    result_evidence: {elapsed_milliseconds: 1250, peak_memory_bytes: 1024},
    status_reason: null,
    timeout_seconds: 3600,
    created_at: "2026-08-28T12:00:00Z",
    updated_at: "2026-08-28T12:01:00Z",
    ...input,
  };
}

function api(initialJobs: ArtifactJob[] = []) {
  const submitted = job({state: "succeeded"});
  return {
    artifactJobCapabilities: vi.fn().mockResolvedValue({
      schema_version: 1,
      transport: {max_input_files: 32, max_input_file_bytes: 536_870_912, max_input_total_bytes: 1_073_741_824, max_output_files: 32, max_output_file_bytes: 1_073_741_824, max_output_total_bytes: 2_147_483_648, max_timeout_seconds: 3600, reserved_input_names: ["manifest.json"]},
      storage: {max_stored_bytes: 10_737_418_240, used_bytes: 1_073_741_824, remaining_bytes: 9_663_676_416},
    }),
    artifactJobsForRun: vi.fn().mockResolvedValue({jobs: initialJobs}),
    createArtifactJob: vi.fn().mockResolvedValue(job({operation_id: null, state: "draft"})),
    uploadArtifactJobInput: vi.fn().mockResolvedValue(job({operation_id: null, state: "draft"})),
    finalizeArtifactJob: vi.fn().mockResolvedValue(job({operation_id: null, state: "ready"})),
    submitArtifactJob: vi.fn().mockResolvedValue(submitted),
    cancelArtifactJob: vi.fn().mockResolvedValue(job({state: "cancelled", status_reason: "Cancelled by operator"})),
    artifactJobResultUrl: vi.fn((jobId: string, digest: string) => `/api/v1/artifact-jobs/${jobId}/results/${digest}`),
  };
}

test("derives prompt, parameter, and input constraints from the running recipe and submits the immutable flow", async () => {
  const user = userEvent.setup();
  const client = api();
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  expect(await screen.findByText("No artifact jobs yet")).toBeInTheDocument();
  expect(screen.getByRole("spinbutton", {name: "Steps"})).toHaveValue(24);
  expect(screen.getByRole("textbox", {name: "Negative Prompt"})).toHaveValue("");
  expect(screen.getByText(/optional visual reference.*image\/png/i)).toBeInTheDocument();
  const submit = screen.getByRole("button", {name: "Submit artifact job"});
  expect(submit).toBeDisabled();

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "A precise titanium part");
  await user.upload(screen.getByLabelText("Reference image"), new File([new Uint8Array(8)], "source.png"));
  expect(submit).toBeEnabled();
  await user.click(submit);

  await waitFor(() => expect(client.submitArtifactJob).toHaveBeenCalled());
  expect(client.submitArtifactJob.mock.calls[0][0]).toBe("00000000-0000-4000-8000-000000000020");
  const create = client.createArtifactJob.mock.calls[0];
  expect(create[0]).toBe(run.run_id);
  expect(create[1]).toMatchObject({
    interface: "image-job",
    parameters: {steps: 24},
    output_limits: {max_files: 3, max_file_bytes: 8_388_608, max_total_bytes: 25_165_824, allowed_media_types: ["image/png", "application/json"]},
    timeout_seconds: 3600,
  });
  expect(create[1].parameters).toEqual({steps: 24, negative_prompt: ""});
  expect(create[1].inputs).toEqual([
    expect.objectContaining({slot: "reference", name: "source.png", media_type: "image/png", size_bytes: 8}),
    expect.objectContaining({slot: "prompt", name: "prompt.txt", media_type: "text/plain", size_bytes: 23}),
  ]);
  expect(client.uploadArtifactJobInput).toHaveBeenCalledWith(
    "00000000-0000-4000-8000-000000000020",
    expect.objectContaining({name: "prompt.txt", sha256: expect.stringMatching(/^[0-9a-f]{64}$/)}),
    expect.any(Blob),
    expect.any(AbortSignal),
    expect.any(Function),
  );
  expect(client.finalizeArtifactJob.mock.calls[0][0]).toBe("00000000-0000-4000-8000-000000000020");
  expect(await screen.findByText("Succeeded")).toBeInTheDocument();
});

test("infers recipe-declared mesh and video media types when the browser omits them", async () => {
  const user = userEvent.setup();
  const client = api();
  const fallbackDetail = detail();
  fallbackDetail.definition = canonicalDefinition([{
      adapter: "artifact-job",
      path: "/outputs",
      input: {path: "/inputs", required: true, media_types: ["model/obj", "model/ply", "video/quicktime", "video/x-matroska"], max_bytes: 16_384, slots: [
        {id: "obj", label: "OBJ mesh", description: "Wavefront mesh.", media_types: ["model/obj"], extensions: [".obj"], min_files: 1, max_files: 1, max_file_bytes: 4096, max_total_bytes: 4096},
        {id: "ply", label: "PLY mesh", description: "Polygon mesh.", media_types: ["model/ply"], extensions: [".ply"], min_files: 1, max_files: 1, max_file_bytes: 4096, max_total_bytes: 4096},
        {id: "mov", label: "MOV video", description: "QuickTime video.", media_types: ["video/quicktime"], extensions: [".mov"], min_files: 1, max_files: 1, max_file_bytes: 4096, max_total_bytes: 4096},
        {id: "mkv", label: "MKV video", description: "Matroska video.", media_types: ["video/x-matroska"], extensions: [".mkv"], min_files: 1, max_files: 1, max_file_bytes: 4096, max_total_bytes: 4096},
      ]},
      output: {path: "/outputs", max_total_bytes: 4096, slots: [
        {id: "result", label: "Result", description: "Job result.", media_types: ["application/json"], extensions: [".json"], min_files: 1, max_files: 1, max_file_bytes: 4096, max_total_bytes: 4096},
      ]},
    }]);
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={fallbackDetail}/>);

  await screen.findByText("No artifact jobs yet");
  await user.upload(screen.getByLabelText("OBJ mesh"), new File(["obj"], "shape.obj"));
  await user.upload(screen.getByLabelText("PLY mesh"), new File(["ply"], "shape.ply"));
  await user.upload(screen.getByLabelText("MOV video"), new File(["mov"], "source.mov"));
  await user.upload(screen.getByLabelText("MKV video"), new File(["mkv"], "source.mkv"));
  const submit = screen.getByRole("button", {name: "Submit artifact job"});
  expect(submit).toBeEnabled();
  await user.click(submit);

  await waitFor(() => expect(client.createArtifactJob).toHaveBeenCalledOnce());
  expect(client.createArtifactJob.mock.calls[0][1].inputs).toEqual([
    expect.objectContaining({slot: "obj", name: "shape.obj", media_type: "model/obj"}),
    expect.objectContaining({slot: "ply", name: "shape.ply", media_type: "model/ply"}),
    expect.objectContaining({slot: "mov", name: "source.mov", media_type: "video/quicktime"}),
    expect.objectContaining({slot: "mkv", name: "source.mkv", media_type: "video/x-matroska"}),
  ]);
});

test("renders every declared job interface as a native bounded form and clears local inputs on change", async () => {
  const user = userEvent.setup();
  const client = api();
  const imageDetail = detail(false);
  const imageInterface = imageDetail.definition.interfaces[0]!;
  const multiple = {
    ...imageDetail,
    definition: {
      ...imageDetail.definition,
      interfaces: [imageInterface, {
        adapter: "video-job",
        path: "/outputs",
        input: {path: "/inputs", required: true, media_types: ["video/mp4"], max_bytes: 4_194_304, min_files: 1, max_files: 1, slots: [
          {id: "source", label: "Source clip", description: "A bounded source video.", media_types: ["video/mp4"], extensions: [".mp4"], min_files: 1, max_files: 1, max_file_bytes: 4_194_304, max_total_bytes: 4_194_304},
        ]},
        output: {path: "/outputs", max_total_bytes: 8_388_608, slots: [
          {id: "video", label: "Generated video", description: "Rendered video result.", media_types: ["video/mp4"], extensions: [".mp4"], min_files: 1, max_files: 1, max_file_bytes: 8_388_608, max_total_bytes: 8_388_608},
        ]},
      }],
    } as RecipeDefinition,
  };
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={multiple}/>);

  await screen.findByRole("button", {name: "Submit artifact job"});
  const selector = screen.getByRole("combobox", {name: "Job interface"});
  expect(within(selector).getByRole("option", {name: "Image Job · 2 bounded slots"})).toBeInTheDocument();
  expect(within(selector).getByRole("option", {name: "Video Job · 1 bounded slot"})).toBeInTheDocument();
  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Unsaved local prompt");
  await user.selectOptions(selector, "1");

  const source = screen.getByLabelText("Source clip");
  expect(source).toHaveAttribute("type", "file");
  expect(source).toHaveAttribute("accept", "video/mp4,.mp4");
  expect(source).toBeRequired();
  expect(source).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText(/1–1 files · 4.0 MiB each · 4.0 MiB total/)).toBeVisible();
  expect(screen.queryByDisplayValue("Unsaved local prompt")).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Submit artifact job"})).toBeDisabled();
});

test("restores durable multi-output results with safe native previews and exact downloads", async () => {
  const image = {name: "frame.png", media_type: "image/png", size_bytes: 2048, sha256: "d".repeat(64)};
  const audio = {name: "sound.wav", media_type: "audio/wav", size_bytes: 4096, sha256: "e".repeat(64)};
  const video = {name: "clip.mp4", media_type: "video/mp4", size_bytes: 6144, sha256: "1".repeat(64)};
  const mesh = {name: "shape.glb", media_type: "model/gltf-binary", size_bytes: 8192, sha256: "f".repeat(64)};
  const client = api([job({output_files: [image, audio, video, mesh]})]);
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  const history = await screen.findByRole("article", {name: /artifact job .* succeeded/i});
  expect(within(history).getByRole("img", {name: "Generated output frame.png"})).toHaveAttribute("src", expect.stringContaining(image.sha256));
  expect(within(history).getByLabelText(/Listen to sound.wav/)).toHaveAttribute("src", expect.stringContaining(audio.sha256));
  expect(within(history).getByLabelText(/Watch clip.mp4/)).toHaveAttribute("src", expect.stringContaining(video.sha256));
  expect(within(history).getByText("3D artifact ready")).toBeInTheDocument();
  expect(within(history).getAllByRole("link", {name: "Download"})).toHaveLength(4);
});

test("lets the operator cancel an in-browser hash before any upload begins", async () => {
  const user = userEvent.setup();
  const client = api();
  vi.mocked(hashArtifactBlob).mockImplementationOnce((_blob, options) => new Promise((_resolve, reject) => {
    options.onProgress?.({loaded: 10, total: 20});
    options.signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), {once: true});
  }));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);
  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Cancel this transfer");
  await user.upload(screen.getByLabelText("Reference image"), new File([new Uint8Array(12)], "reference.png", {type: "image/png"}));
  expect(screen.getByRole("list", {name: "Selected input files"})).toBeInTheDocument();
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));
  expect(await screen.findByText("10 B of 32 B")).toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Cancel transfer"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Submission cancelled");
  expect(screen.queryByRole("list", {name: "Selected input files"})).not.toBeInTheDocument();
  expect(client.createArtifactJob).not.toHaveBeenCalled();
  expect(client.uploadArtifactJobInput).not.toHaveBeenCalled();
});

test("aborts an active upload and cancels the durable draft on the controller", async () => {
  const user = userEvent.setup();
  const client = api();
  client.uploadArtifactJobInput.mockImplementationOnce((_jobId, _file, _blob, signal, onProgress) => new Promise((_resolve, reject) => {
    onProgress?.({loaded: 5, total: 64});
    signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), {once: true});
  }));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);
  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Cancel this upload now");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByText(/^5 B of /)).toBeInTheDocument();
  expect(client.createArtifactJob).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", {name: "Cancel transfer"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("Submission cancelled");
  await waitFor(() => expect(client.cancelArtifactJob).toHaveBeenCalledWith(
    "00000000-0000-4000-8000-000000000020",
    "Cancelled by operator during browser transfer",
  ));
});

test("keeps the contract visible without a run and provides explicit cancel confirmation and retry recovery", async () => {
  const user = userEvent.setup();
  const queued = job({state: "queued", output_manifest_sha256: null, result_evidence: null});
  const client = api([queued]);
  const {rerender} = render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail(false)}/>);
  expect(screen.getByText("No running recipe")).toBeInTheDocument();
  expect(await screen.findByRole("button", {name: "Submit artifact job"})).toBeDisabled();

  rerender(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);
  const cancel = await screen.findByRole("button", {name: "Cancel job"});
  await user.click(cancel);
  expect(screen.getByText("Cancel this job and keep its audit history?")).toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Confirm cancel"}));
  expect(await screen.findByText("Cancelled")).toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Prepare retry"}));
  expect(screen.getByRole("status")).toHaveTextContent("reselect local inputs");
});

test("keeps submission disabled and gives operators explicit recovery for preflight and history failures", async () => {
  const user = userEvent.setup();
  const client = api();
  client.artifactJobCapabilities.mockRejectedValueOnce(new Error("capacity service offline"));
  client.artifactJobsForRun.mockRejectedValueOnce(new Error("history temporarily unavailable"));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  expect(await screen.findByText("Storage preflight unavailable")).toBeInTheDocument();
  expect(screen.getByText("capacity service offline")).toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Storage preflight required"})).toBeDisabled();
  expect(await screen.findByText("Job history unavailable")).toBeInTheDocument();

  await user.click(screen.getByRole("button", {name: "Retry storage preflight"}));
  await user.click(screen.getByRole("button", {name: "Retry history"}));

  expect(await screen.findByText("No artifact jobs yet")).toBeInTheDocument();
  expect(await screen.findByRole("button", {name: "Submit artifact job"})).toBeDisabled();
  expect(client.artifactJobCapabilities).toHaveBeenCalledTimes(2);
  expect(client.artifactJobsForRun).toHaveBeenCalledTimes(2);
});

test("keeps the declared native form visible while controller capacity is loading", async () => {
  let resolveCapabilities!: (value: Awaited<ReturnType<ReturnType<typeof api>["artifactJobCapabilities"]>>) => void;
  const client = api();
  const capability = await client.artifactJobCapabilities();
  client.artifactJobCapabilities.mockReset();
  client.artifactJobCapabilities.mockImplementationOnce(() => new Promise(resolve => { resolveCapabilities = resolve; }));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail(false)}/>);

  expect(screen.getByRole("textbox", {name: "Prompt"})).toBeVisible();
  expect(screen.getByLabelText("Reference image")).toHaveAttribute("type", "file");
  expect(screen.getByRole("button", {name: "Checking controller capacity…"})).toBeDisabled();
  resolveCapabilities(capability);
  expect(await screen.findByRole("button", {name: "Submit artifact job"})).toBeDisabled();
});

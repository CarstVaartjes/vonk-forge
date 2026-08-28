import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ArtifactJob, LibraryApi, LibraryRecipeDetail} from "../api/types";
import {fullLibraryDetail} from "../test-fixtures/library";
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
  return {
    ...fullLibraryDetail,
    visual_recipe: ({
      ...fullLibraryDetail.visual_recipe!,
      interfaces: [{
        adapter: "image-job",
        path: "/outputs",
        input: {path: "/inputs", required: true, media_types: ["text/plain", "image/png"], max_bytes: 16_384, slots: [
          {id: "prompt", label: "Prompt", description: "A UTF-8 generation prompt.", media_types: ["text/plain"], extensions: [".txt"], min_files: 1, max_files: 1, max_file_bytes: 16_384, max_total_bytes: 16_384},
          {id: "reference", label: "Reference image", description: "An optional visual reference.", media_types: ["image/png"], extensions: [".png"], min_files: 0, max_files: 1, max_file_bytes: 8_388_608, max_total_bytes: 8_388_608},
        ]},
        output: {path: "/outputs", allowed_media_types: ["image/png", "application/json"], max_total_bytes: 25_165_824, slots: [
          {id: "images", label: "Generated images", description: "Rendered image results.", media_types: ["image/png"], extensions: [".png"], min_files: 1, max_files: 2, max_file_bytes: 8_388_608, max_total_bytes: 16_777_216},
          {id: "metadata", label: "Metadata", description: "Generation metadata.", media_types: ["application/json"], extensions: [".json"], min_files: 0, max_files: 1, max_file_bytes: 8_388_608, max_total_bytes: 8_388_608},
        ]},
      }],
      parameters: [{
        name: "steps", description: "Number of denoising steps.", type: "integer", default: 24,
        minimum: 1, maximum: 50, allowed_values: [], pattern: null, change_effect: "restart",
      }, {
        name: "negative_prompt", description: "Optional concepts to avoid.", type: "string", default: null,
        pattern: null, change_effect: "restart",
      }],
    } as unknown as LibraryRecipeDetail["visual_recipe"]),
    operational_state: {...fullLibraryDetail.operational_state, runs: running ? [run] : []},
  };
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
  expect(create[1].parameters).toEqual({steps: 24});
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

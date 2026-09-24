import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ArtifactJob, LibraryApi, LibraryViewRecipeDetail, RecipeDefinition} from "../api/types";
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

function detail(running = true): LibraryViewRecipeDetail {
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
  } as unknown as LibraryViewRecipeDetail;
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
    submit_request_id: "00000000-0000-4000-8000-000000000099",
    interface: "image-job",
    state: "succeeded",
    contract_sha256: "8".repeat(64),
    compiled_contract: {
      schema_version: 1,
      interface: "image-job",
      input: canonicalImageInterface().input,
      parameters: [],
      output: canonicalImageInterface().output,
      output_limits: {max_files: 3, max_file_bytes: 8_388_608, max_total_bytes: 25_165_824, allowed_media_types: ["image/png", "application/json"]},
      max_timeout_seconds: 3600,
    },
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
  let storedDraft: ArtifactJob | undefined;
  return {
    artifactJobCapabilities: vi.fn().mockResolvedValue({
      schema_version: 1,
      transport: {max_input_files: 32, max_input_file_bytes: 536_870_912, max_input_total_bytes: 1_073_741_824, max_output_files: 32, max_output_file_bytes: 1_073_741_824, max_output_total_bytes: 2_147_483_648, max_timeout_seconds: 3600, reserved_input_names: ["manifest.json"]},
      storage: {max_stored_bytes: 10_737_418_240, used_bytes: 1_073_741_824, remaining_bytes: 9_663_676_416},
    }),
    artifactJobsForRun: vi.fn().mockResolvedValue({jobs: initialJobs}),
    artifactJobByRequestId: vi.fn().mockRejectedValue(Object.assign(new Error("artifact job request not found"), {status: 404})),
    createArtifactJob: vi.fn().mockImplementation((runId: string, input: Parameters<LibraryApi["createArtifactJob"]>[1]) => {
      storedDraft = job({run_id: runId, operation_id: null, submit_request_id: null, state: "draft", input_declarations: input.inputs ?? [], input_files: []});
      return Promise.resolve(storedDraft);
    }),
    uploadArtifactJobInput: vi.fn().mockImplementation((_jobId: string, file: Parameters<LibraryApi["uploadArtifactJobInput"]>[1]) => {
      storedDraft = job({...storedDraft, operation_id: null, submit_request_id: null, state: "draft", input_files: [...(storedDraft?.input_files ?? []), file]});
      return Promise.resolve(storedDraft);
    }),
    finalizeArtifactJob: vi.fn().mockImplementation(() => {
      storedDraft = job({...storedDraft, operation_id: null, submit_request_id: null, state: "ready"});
      return Promise.resolve(storedDraft);
    }),
    artifactJob: vi.fn().mockResolvedValue(job()),
    submitArtifactJob: vi.fn().mockImplementation((_jobId: string, requestId: string) => Promise.resolve({...submitted, submit_request_id: requestId})),
    cancelArtifactJob: vi.fn().mockImplementation((_jobId: string, reason: string, requestId: string) => Promise.resolve(job({state: "cancelled", status_reason: reason, result_evidence: {elapsed_milliseconds: 1250, peak_memory_bytes: 1024, cancel_request_id: requestId, cancel_reason: reason}}))),
    artifactJobResultUrl: vi.fn(
      (jobId: string, name: string, digest: string) =>
        `/api/artifact-jobs/${jobId}/results/${encodeURIComponent(name)}/${digest}`,
    ),
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

test("reuses the create identity across lost receipts, resumed upload, and exact submit recovery", async () => {
  const user = userEvent.setup();
  const client = api();
  const draft = job({operation_id: null, state: "draft", submit_request_id: null, input_files: []});
  client.createArtifactJob.mockRejectedValueOnce(new Error("connection closed after create"));
  client.artifactJobByRequestId.mockResolvedValueOnce(draft);
  client.createArtifactJob
    .mockResolvedValueOnce(draft)
    .mockImplementationOnce((_runId, body) => Promise.resolve(job({
      operation_id: null,
      state: "draft",
      submit_request_id: null,
      input_files: body.inputs,
    })))
    .mockImplementationOnce((_runId, body) => Promise.resolve(job({
      operation_id: null,
      state: "ready",
      submit_request_id: null,
      input_files: body.inputs,
    })));
  client.uploadArtifactJobInput.mockRejectedValueOnce(new Error("upload receipt was lost"));
  client.finalizeArtifactJob.mockRejectedValueOnce(new Error("finalize receipt was lost"));
  client.submitArtifactJob.mockImplementationOnce((_jobId: string, requestId: string) => {
    client.artifactJob.mockResolvedValueOnce(job({
      state: "queued",
      operation_id: "operation-accepted",
      submit_request_id: requestId,
    }));
    return Promise.reject(new Error("connection closed after submit"));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Retry the exact draft");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));
  expect(await screen.findByText("upload receipt was lost")).toBeInTheDocument();
  const firstCreateKey = client.createArtifactJob.mock.calls[0]![2];
  expect(firstCreateKey).toMatch(/^[0-9a-f-]{36}$/);
  expect(client.artifactJobByRequestId).toHaveBeenCalledWith(firstCreateKey);
  expect(client.createArtifactJob.mock.calls[1]![2]).toBe(firstCreateKey);

  await user.click(screen.getByRole("button", {name: "Review and try again"}));
  await user.click(screen.getByRole("button", {name: "Submit artifact job"}));
  expect(await screen.findByText("finalize receipt was lost")).toBeInTheDocument();
  expect(client.uploadArtifactJobInput).toHaveBeenCalledOnce();

  await user.click(screen.getByRole("button", {name: "Review and try again"}));
  await user.click(screen.getByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByText("Queued")).toBeInTheDocument();
  expect(client.createArtifactJob).toHaveBeenCalledTimes(4);
  expect(client.createArtifactJob.mock.calls.map(call => call[2])).toEqual([
    firstCreateKey,
    firstCreateKey,
    firstCreateKey,
    firstCreateKey,
  ]);
  expect(client.finalizeArtifactJob).toHaveBeenCalledOnce();
  expect(client.submitArtifactJob).toHaveBeenCalledOnce();
  const submitKey = client.submitArtifactJob.mock.calls[0]![1];
  expect(submitKey).toMatch(/^[0-9a-f-]{36}$/);
  expect(client.artifactJob).toHaveBeenCalledWith("00000000-0000-4000-8000-000000000020");
});

test("does not let a matching old submit receipt override an explicit permission denial", async () => {
  const user = userEvent.setup();
  const client = api();
  client.submitArtifactJob.mockImplementationOnce((_jobId: string, requestId: string) => {
    client.artifactJob.mockResolvedValueOnce(job({state: "queued", operation_id: "old-operation", submit_request_id: requestId}));
    return Promise.reject(Object.assign(new Error("permission denied"), {status: 403}));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Do not bypass a denial");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("permission denied");
  expect(client.artifactJob).not.toHaveBeenCalled();
  expect(screen.queryByText("Queued")).not.toBeInTheDocument();
});

test("does not retry an aborted create after the exact request lookup returns not found", async () => {
  const user = userEvent.setup();
  const client = api();
  client.createArtifactJob.mockImplementationOnce((_runId: string, _body, _requestId: string, signal?: AbortSignal) => new Promise((_resolve, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), {once: true});
  }));
  client.artifactJobByRequestId.mockRejectedValueOnce(Object.assign(new Error("not found"), {status: 404}));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Stop before retry");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));
  await waitFor(() => expect(client.createArtifactJob).toHaveBeenCalledOnce());
  await user.click(screen.getByRole("button", {name: "Cancel transfer"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("before a durable artifact job was created");
  expect(client.artifactJobByRequestId).toHaveBeenCalledOnce();
  expect(client.createArtifactJob).toHaveBeenCalledOnce();
  expect(client.uploadArtifactJobInput).not.toHaveBeenCalled();
  expect(client.cancelArtifactJob).not.toHaveBeenCalled();
});

test("cancels the exact draft found after an aborted create without replaying create", async () => {
  const user = userEvent.setup();
  const client = api();
  const acceptedDraft = job({id: "00000000-0000-4000-8000-000000000044", run_id: run.run_id, operation_id: null, state: "draft", submit_request_id: null, input_files: []});
  client.createArtifactJob.mockImplementationOnce((_runId: string, _body, _requestId: string, signal?: AbortSignal) => new Promise((_resolve, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), {once: true});
  }));
  client.artifactJobByRequestId.mockResolvedValueOnce(acceptedDraft);
  client.cancelArtifactJob.mockImplementationOnce((_jobId: string, reason: string, requestId: string) => Promise.resolve(job({
    id: acceptedDraft.id,
    run_id: acceptedDraft.run_id,
    state: "cancelled",
    status_reason: reason,
    result_evidence: {elapsed_milliseconds: 1, peak_memory_bytes: 1, cancel_request_id: requestId, cancel_reason: reason},
  })));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Cancel the accepted create");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));
  await waitFor(() => expect(client.createArtifactJob).toHaveBeenCalledOnce());
  await user.click(screen.getByRole("button", {name: "Cancel transfer"}));

  expect(await screen.findByRole("status")).toHaveTextContent(`Controller cancelled draft ${acceptedDraft.id}`);
  expect(client.createArtifactJob).toHaveBeenCalledOnce();
  expect(client.cancelArtifactJob).toHaveBeenCalledWith(
    acceptedDraft.id,
    "Cancelled by operator during browser transfer",
    expect.stringMatching(/^[0-9a-f-]{36}$/),
  );
});

test("does not replay submit after cancellation when the exact job is still ready", async () => {
  const user = userEvent.setup();
  const client = api();
  client.submitArtifactJob.mockImplementationOnce((_jobId: string, _requestId: string, signal?: AbortSignal) => new Promise((_resolve, reject) => {
    signal?.addEventListener("abort", () => {
      client.artifactJob.mockResolvedValueOnce(job({state: "ready", operation_id: null, submit_request_id: null}));
      reject(new DOMException("cancelled", "AbortError"));
    }, {once: true});
  }));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Stop the submit retry");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));
  await screen.findByText("Submitting to the Spark…");
  await user.click(screen.getByRole("button", {name: "Cancel transfer"}));

  expect(await screen.findByText(/Controller cancelled draft/)).toBeInTheDocument();
  expect(client.submitArtifactJob).toHaveBeenCalledOnce();
  expect(client.cancelArtifactJob).toHaveBeenCalledOnce();
});

test("does not follow a create response that belongs to another run", async () => {
  const user = userEvent.setup();
  const client = api();
  client.createArtifactJob.mockResolvedValueOnce(job({run_id: "00000000-0000-4000-8000-000000000099", operation_id: null, state: "draft", submit_request_id: null, input_files: []}));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Wrong run response");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("another recipe run");
  expect(client.uploadArtifactJobInput).not.toHaveBeenCalled();
  expect(client.finalizeArtifactJob).not.toHaveBeenCalled();
});

test("does not finalize an upload response for a different job", async () => {
  const user = userEvent.setup();
  const client = api();
  client.uploadArtifactJobInput.mockImplementationOnce((_jobId, file) => Promise.resolve(job({
    id: "00000000-0000-4000-8000-000000000055",
    run_id: run.run_id,
    operation_id: null,
    state: "draft",
    submit_request_id: null,
    input_files: [file],
  })));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Wrong job upload response");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("Input upload returned another artifact job");
  expect(client.finalizeArtifactJob).not.toHaveBeenCalled();
  expect(client.submitArtifactJob).not.toHaveBeenCalled();
});

test("reconciles a malformed submit response only through the exact job identity", async () => {
  const user = userEvent.setup();
  const client = api();
  client.submitArtifactJob.mockImplementationOnce((_jobId: string, requestId: string) => {
    client.artifactJob.mockResolvedValueOnce(job({
      id: "00000000-0000-4000-8000-000000000020",
      run_id: run.run_id,
      state: "queued",
      operation_id: "operation-accepted",
      submit_request_id: requestId,
    }));
    return Promise.resolve(job({
      id: "00000000-0000-4000-8000-000000000056",
      run_id: run.run_id,
      state: "queued",
      operation_id: "operation-accepted",
      submit_request_id: requestId,
    }));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Verify returned job identity");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByText("Queued")).toBeInTheDocument();
  expect(client.artifactJob).toHaveBeenCalledWith("00000000-0000-4000-8000-000000000020");
});

test("refuses a submit lookup receipt from another run", async () => {
  const user = userEvent.setup();
  const client = api();
  client.submitArtifactJob.mockImplementationOnce((_jobId: string, requestId: string) => {
    client.artifactJob.mockResolvedValueOnce(job({
      id: "00000000-0000-4000-8000-000000000020",
      run_id: "00000000-0000-4000-8000-000000000099",
      state: "queued",
      operation_id: "operation-accepted",
      submit_request_id: requestId,
    }));
    return Promise.reject(new Error("connection closed after submit"));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Reject foreign run receipt");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("another recipe run");
  expect(screen.queryByText("Queued")).not.toBeInTheDocument();
});

test("does not reconcile an explicit cancellation denial from a matching old receipt", async () => {
  const user = userEvent.setup();
  const queued = job({state: "queued", result_evidence: null});
  const client = api([queued]);
  client.cancelArtifactJob.mockImplementationOnce((_jobId: string, reason: string, requestId: string) => {
    client.artifactJob.mockResolvedValueOnce(job({
      id: queued.id,
      run_id: queued.run_id,
      state: "cancelled",
      status_reason: reason,
      result_evidence: {elapsed_milliseconds: 1, peak_memory_bytes: 1, cancel_request_id: requestId, cancel_reason: reason},
    }));
    return Promise.reject(Object.assign(new Error("permission denied"), {status: 403}));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.click(await screen.findByRole("button", {name: "Cancel job"}));
  await user.click(screen.getByRole("button", {name: "Confirm cancel"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("permission denied");
  expect(client.artifactJob).not.toHaveBeenCalled();
  expect(screen.getByText("Queued")).toBeInTheDocument();
});

test("reconciles a malformed cancel response only through the requested job and run", async () => {
  const user = userEvent.setup();
  const queued = job({state: "queued", result_evidence: null});
  const client = api([queued]);
  client.cancelArtifactJob.mockImplementationOnce((jobId: string, reason: string, requestId: string) => {
    const evidence = {elapsed_milliseconds: 1, peak_memory_bytes: 1, cancel_request_id: requestId, cancel_reason: reason};
    client.artifactJob.mockResolvedValueOnce(job({id: jobId, run_id: queued.run_id, state: "cancelled", status_reason: reason, result_evidence: evidence}));
    return Promise.resolve(job({id: jobId, run_id: "00000000-0000-4000-8000-000000000099", state: "cancelled", status_reason: reason, result_evidence: evidence}));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.click(await screen.findByRole("button", {name: "Cancel job"}));
  await user.click(screen.getByRole("button", {name: "Confirm cancel"}));

  expect(await screen.findByText("Cancelled")).toBeInTheDocument();
  expect(client.artifactJob).toHaveBeenCalledWith(queued.id);
});

test("does not accept a cancel lookup receipt for a different job", async () => {
  const user = userEvent.setup();
  const queued = job({state: "queued", result_evidence: null});
  const client = api([queued]);
  client.cancelArtifactJob.mockImplementationOnce((_jobId: string, reason: string, requestId: string) => {
    client.artifactJob.mockResolvedValueOnce(job({
      id: "00000000-0000-4000-8000-000000000055",
      run_id: queued.run_id,
      state: "cancelled",
      status_reason: reason,
      result_evidence: {elapsed_milliseconds: 1, peak_memory_bytes: 1, cancel_request_id: requestId, cancel_reason: reason},
    }));
    return Promise.reject(new Error("connection closed after cancel"));
  });
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.click(await screen.findByRole("button", {name: "Cancel job"}));
  await user.click(screen.getByRole("button", {name: "Confirm cancel"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("another artifact job");
  expect(screen.getByText("Queued")).toBeInTheDocument();
});

test("ignores another job's pending cancellation when admitting a new independent draft", async () => {
  const user = userEvent.setup();
  const pending = job({state: "cancelling", result_evidence: {elapsed_milliseconds: 1, peak_memory_bytes: 1, cancel_request_id: "00000000-0000-4000-8000-000000000077", cancel_reason: "stop"}});
  const client = api([pending]);
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Independent request");
  const submit = await screen.findByRole("button", {name: "Submit artifact job"});
  expect(submit).toBeEnabled();
  await user.click(submit);

  expect(await screen.findByText("Succeeded")).toBeInTheDocument();
  expect(client.createArtifactJob).toHaveBeenCalledOnce();
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
  const metadata = {
    name: "frame-metadata.json",
    media_type: "application/json",
    size_bytes: 2048,
    sha256: image.sha256,
  };
  const audio = {name: "sound.wav", media_type: "audio/wav", size_bytes: 4096, sha256: "e".repeat(64)};
  const video = {name: "clip.mp4", media_type: "video/mp4", size_bytes: 6144, sha256: "1".repeat(64)};
  const mesh = {name: "shape.glb", media_type: "model/gltf-binary", size_bytes: 8192, sha256: "f".repeat(64)};
  const client = api([job({output_files: [image, metadata, audio, video, mesh]})]);
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  const history = await screen.findByRole("article", {name: /artifact job .* succeeded/i});
  expect(
    within(history).getByRole("img", {name: "Generated output frame.png"}),
  ).toHaveAttribute(
    "src",
    `/api/artifact-jobs/00000000-0000-4000-8000-000000000020/results/${image.name}/${image.sha256}`,
  );
  expect(within(history).getByLabelText(/Listen to sound.wav/)).toHaveAttribute("src", expect.stringContaining(audio.sha256));
  expect(within(history).getByLabelText(/Watch clip.mp4/)).toHaveAttribute("src", expect.stringContaining(video.sha256));
  expect(within(history).getByText("3D artifact ready")).toBeInTheDocument();
  const downloads = within(history).getAllByRole("link", {name: "Download"});
  expect(downloads).toHaveLength(5);
  expect(downloads[0]).toHaveAttribute(
    "href",
    `/api/artifact-jobs/00000000-0000-4000-8000-000000000020/results/${image.name}/${image.sha256}`,
  );
  expect(downloads[1]).toHaveAttribute(
    "href",
    `/api/artifact-jobs/00000000-0000-4000-8000-000000000020/results/${metadata.name}/${metadata.sha256}`,
  );
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
  expect(await screen.findByRole("alert")).toHaveTextContent("before a durable artifact job was created");
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

  expect(await screen.findByText(/Controller cancelled draft/)).toBeInTheDocument();
  await waitFor(() => expect(client.cancelArtifactJob).toHaveBeenCalledWith(
    "00000000-0000-4000-8000-000000000020",
    "Cancelled by operator during browser transfer",
    expect.stringMatching(/^[0-9a-f-]{36}$/),
  ));
});

test("surfaces an unconfirmed upload cancellation and retries with the same request identity", async () => {
  const user = userEvent.setup();
  const client = api();
  client.uploadArtifactJobInput.mockImplementationOnce((_jobId, _file, _blob, signal, onProgress) => new Promise((_resolve, reject) => {
    onProgress?.({loaded: 5, total: 64});
    signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), {once: true});
  }));
  client.cancelArtifactJob
    .mockRejectedValueOnce(new Error("connection closed after cancel"))
    .mockRejectedValueOnce(new Error("Controller still unreachable"));
  client.artifactJob
    .mockResolvedValueOnce(job({state: "draft", operation_id: null, submit_request_id: null}))
    .mockResolvedValueOnce(job({state: "draft", operation_id: null, submit_request_id: null}));
  render(<ArtifactJobWorkspace api={client as unknown as LibraryApi} detail={detail()}/>);

  await user.type(screen.getByRole("textbox", {name: "Prompt"}), "Cancel this durable draft");
  await user.click(await screen.findByRole("button", {name: "Submit artifact job"}));
  await screen.findByText(/^5 B of /);
  await user.click(screen.getByRole("button", {name: "Cancel transfer"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("Cancellation needs recovery");
  expect(screen.getByRole("alert")).toHaveTextContent("without this cancellation receipt");
  expect(screen.getByRole("button", {name: "Submit artifact job"})).toBeDisabled();
  await user.click(screen.getByRole("button", {name: "Retry cancellation"}));
  expect(await screen.findByText(/Controller cancelled draft/)).toBeInTheDocument();
  const cancelCalls = client.cancelArtifactJob.mock.calls;
  expect(cancelCalls).toHaveLength(3);
  expect(cancelCalls.map(call => call[2])).toEqual([cancelCalls[0]![2], cancelCalls[0]![2], cancelCalls[0]![2]]);
  expect(cancelCalls.map(call => call[1])).toEqual([
    "Cancelled by operator during browser transfer",
    "Cancelled by operator during browser transfer",
    "Cancelled by operator during browser transfer",
  ]);
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

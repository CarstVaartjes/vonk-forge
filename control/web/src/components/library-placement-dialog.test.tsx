import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import type {
  LibraryApi,
  LibraryPlacementApplication,
  LibraryPlacementApplyInput,
  LibraryPlacementPreview,
  LibraryPlacementPreviewInput,
} from "../api/types";
import {LibraryPlacementDialog} from "./library-placement-dialog";

const RECIPE_ID = "00000000-0000-4000-8000-000000000001";
const REVISION_ID = "00000000-0000-4000-8000-000000000002";
const PLACEMENT_ID = "00000000-0000-4000-8000-000000000003";
const OPERATION_ID = "00000000-0000-4000-8000-000000000004";
const NODE_A = `spk_${"a".repeat(32)}`;
const NODE_B = `spk_${"b".repeat(32)}`;

function placementPreview(overrides: Partial<LibraryPlacementPreview> = {}): LibraryPlacementPreview {
  return {
    schema_version: 1,
    generated_at: "2026-09-01T12:00:00Z",
    recipe_id: RECIPE_ID,
    recipe_revision_id: REVISION_ID,
    recipe_title: "Qwen Chat",
    topology_name: "pair",
    desired_state: "installed",
    alias: null,
    invocation: "button",
    selected_node_ids: [NODE_A, NODE_B],
    selected_nodes: [
      {
        node_id: NODE_A,
        rank: 0,
        role: "leader",
        endpoint_owner: true,
        disk_free_bytes: 400,
        disk_required_bytes: 100,
        disk_free_after_bytes: 300,
        memory_available_bytes: 300,
        memory_required_bytes: 100,
        memory_free_after_bytes: 200,
      },
      {
        node_id: NODE_B,
        rank: 1,
        role: "worker",
        endpoint_owner: false,
        disk_free_bytes: 400,
        disk_required_bytes: 100,
        disk_free_after_bytes: 300,
        memory_available_bytes: 300,
        memory_required_bytes: 100,
        memory_free_after_bytes: 200,
      },
    ],
    allowed: true,
    steps: [{index: 0, kind: "install", label: "Install Qwen Chat", node_ids: [NODE_A, NODE_B]}],
    blockers: [],
    warnings: [],
    locations: {installation_ids: [], run_ids: [], installed: false, running: false},
    plan_digest: "a".repeat(64),
    ...overrides,
  };
}

function placementApplication(
  state: LibraryPlacementApplication["state"],
  overrides: Partial<LibraryPlacementApplication> = {},
): LibraryPlacementApplication {
  return {
    schema_version: 1,
    id: PLACEMENT_ID,
    state,
    recipe_id: RECIPE_ID,
    recipe_revision_id: REVISION_ID,
    selected_node_ids: [NODE_A, NODE_B],
    desired_state: "installed",
    alias: null,
    plan_digest: "a".repeat(64),
    current_step: 0,
    total_steps: 3,
    current_operation_id: null,
    status_reason: null,
    progress: {completed_steps: 0, total_steps: 3},
    locations: {installation_ids: [], run_ids: [], installed: false, running: false},
    created_at: "2026-09-01T12:00:00Z",
    updated_at: "2026-09-01T12:00:00Z",
    ...overrides,
  };
}

function renderDialog(api: LibraryApi, overrides: Partial<React.ComponentProps<typeof LibraryPlacementDialog>> = {}) {
  return render(<LibraryPlacementDialog
    api={api}
    invocation="button"
    nodeIds={[NODE_B, NODE_A]}
    nodeNames={{[NODE_A]: "Spark A", [NODE_B]: "Spark B"}}
    onClose={vi.fn()}
    onRefresh={vi.fn(async () => undefined)}
    recipeId={RECIPE_ID}
    recipeTitle="Qwen Chat"
    {...overrides}
  />);
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function enabledApplyButton() {
  const button = await screen.findByRole("button", {name: "Install on selected Sparks"});
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("applies the exact digest and reuses one request key after an ambiguous failure", async () => {
  const plan = placementPreview();
  const applyInputs: LibraryPlacementApplyInput[] = [];
  const applyLibraryPlacement = vi.fn(async (input: LibraryPlacementApplyInput) => {
    applyInputs.push(input);
    if (applyInputs.length === 1) throw new Error("connection closed after apply");
    return placementApplication("queued");
  });
  const api = {
    previewLibraryPlacement: vi.fn(async (input: LibraryPlacementPreviewInput) => {
      expect(input.node_ids).toEqual([NODE_A, NODE_B]);
      return plan;
    }),
    applyLibraryPlacement,
    libraryPlacement: vi.fn(() => new Promise<LibraryPlacementApplication>(() => undefined)),
  } as unknown as LibraryApi;
  renderDialog(api);

  const apply = await enabledApplyButton();
  fireEvent.click(apply);
  expect(await screen.findByRole("alert")).toHaveTextContent("connection closed after apply");
  expect(screen.getByText(/Retry uses the same request key/)).toBeVisible();
  fireEvent.click(apply);

  expect(await screen.findByText("Queued")).toBeVisible();
  expect(applyLibraryPlacement).toHaveBeenCalledTimes(2);
  expect(applyInputs[0]).toMatchObject({
    node_ids: [NODE_A, NODE_B],
    plan_digest: plan.plan_digest,
    recipe_id: RECIPE_ID,
  });
  expect(applyInputs[1].request_key).toBe(applyInputs[0].request_key);
});

test("requires a fresh digest and request key after the Controller rejects a stale preview", async () => {
  const oldPlan = placementPreview();
  const freshPlan = placementPreview({plan_digest: "b".repeat(64), steps: [{index: 0, kind: "keep", label: "Keep current installation", node_ids: [NODE_A, NODE_B]}]});
  const previewLibraryPlacement = vi.fn()
    .mockResolvedValueOnce(oldPlan)
    .mockResolvedValueOnce(freshPlan);
  const applyInputs: LibraryPlacementApplyInput[] = [];
  const applyLibraryPlacement = vi.fn(async (input: LibraryPlacementApplyInput) => {
    applyInputs.push(input);
    if (applyInputs.length === 1) throw new Error("Library placement preview is stale");
    return placementApplication("succeeded", {plan_digest: freshPlan.plan_digest});
  });
  const api = {previewLibraryPlacement, applyLibraryPlacement, libraryPlacement: vi.fn()} as unknown as LibraryApi;
  renderDialog(api);

  fireEvent.click(await enabledApplyButton());
  expect(await screen.findByText(/Review a fresh plan before applying/)).toBeVisible();
  expect(screen.getByRole("button", {name: "Install on selected Sparks"})).toBeDisabled();
  fireEvent.click(screen.getByRole("button", {name: "Review fresh plan"}));
  expect(await screen.findByText("Keep current installation")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Install on selected Sparks"}));

  expect(await screen.findByText("Succeeded")).toBeVisible();
  expect(applyInputs[1].plan_digest).toBe(freshPlan.plan_digest);
  expect(applyInputs[1].request_key).not.toBe(applyInputs[0].request_key);
});

test("renders durable queued, running, waiting, and succeeded progress with terminal locations", async () => {
  const onRefresh = vi.fn(async () => undefined);
  const libraryPlacement = vi.fn()
    .mockResolvedValueOnce(placementApplication("running", {
      current_step: 1,
      current_operation_id: OPERATION_ID,
      progress: {completed_steps: 1, total_steps: 3},
    }))
    .mockResolvedValueOnce(placementApplication("waiting-for-operator", {
      current_step: 2,
      progress: {completed_steps: 2, total_steps: 3},
      status_reason: "Confirm recovery on the Spark.",
    }))
    .mockResolvedValueOnce(placementApplication("succeeded", {
      current_step: 3,
      progress: {completed_steps: 3, total_steps: 3},
      locations: {installation_ids: ["00000000-0000-4000-8000-000000000005"], run_ids: [], installed: true, running: false},
    }));
  const api = {
    previewLibraryPlacement: vi.fn(async () => placementPreview()),
    applyLibraryPlacement: vi.fn(async () => placementApplication("queued")),
    libraryPlacement,
  } as unknown as LibraryApi;
  renderDialog(api, {onRefresh});

  const apply = await enabledApplyButton();
  vi.useFakeTimers();
  fireEvent.click(apply);
  await flushPromises();
  expect(screen.getByText("Queued")).toBeVisible();

  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(screen.getByText("Running")).toBeVisible();
  expect(screen.getByRole("progressbar", {name: "Placement steps completed"})).toHaveAttribute("aria-valuenow", "1");

  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(screen.getByText("Waiting for operator")).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("Confirm recovery on the Spark.");

  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(screen.getByText("Succeeded")).toBeVisible();
  expect(screen.getByText("Installed on Spark A + Spark B")).toBeVisible();
  await flushPromises();
  expect(onRefresh).toHaveBeenCalledWith(expect.any(AbortSignal));
  expect(libraryPlacement).toHaveBeenCalledTimes(3);
});

test("offers a fresh recovery review after a failed durable placement", async () => {
  const previewLibraryPlacement = vi.fn(async () => placementPreview());
  const onRefresh = vi.fn(async () => undefined);
  const api = {
    previewLibraryPlacement,
    applyLibraryPlacement: vi.fn(async () => placementApplication("failed", {status_reason: "The worker did not become ready."})),
    libraryPlacement: vi.fn(),
  } as unknown as LibraryApi;
  renderDialog(api, {onRefresh});

  fireEvent.click(await enabledApplyButton());
  expect(await screen.findByRole("alert")).toHaveTextContent("The worker did not become ready.");
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", {name: "Review recovery plan"}));

  await waitFor(() => expect(previewLibraryPlacement).toHaveBeenCalledTimes(2));
  expect(await screen.findByText("Install Qwen Chat")).toBeVisible();
  expect(screen.getByRole("button", {name: "Install on selected Sparks"})).toBeEnabled();
});

test("keeps last-known progress through a poll error and retries in place", async () => {
  const onRefresh = vi.fn(async () => undefined);
  const libraryPlacement = vi.fn()
    .mockRejectedValueOnce(new Error("progress authority unavailable"))
    .mockResolvedValueOnce(placementApplication("succeeded", {
      current_step: 3,
      progress: {completed_steps: 3, total_steps: 3},
    }));
  const api = {
    previewLibraryPlacement: vi.fn(async () => placementPreview()),
    applyLibraryPlacement: vi.fn(async () => placementApplication("queued")),
    libraryPlacement,
  } as unknown as LibraryApi;
  renderDialog(api, {onRefresh});

  const apply = await enabledApplyButton();
  vi.useFakeTimers();
  fireEvent.click(apply);
  await flushPromises();
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(screen.getByText("Queued")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("progress authority unavailable");

  fireEvent.click(screen.getByRole("button", {name: "Retry progress"}));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("Succeeded")).toBeVisible();
  expect(screen.queryByText(/Progress is temporarily unavailable/)).not.toBeInTheDocument();
  await flushPromises();
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

test("retries a terminal Library refresh without restarting placement progress", async () => {
  const onRefresh = vi.fn()
    .mockRejectedValueOnce(new Error("Fleet refresh unavailable"))
    .mockResolvedValueOnce(undefined);
  const libraryPlacement = vi.fn();
  const api = {
    previewLibraryPlacement: vi.fn(async () => placementPreview()),
    applyLibraryPlacement: vi.fn(async () => placementApplication("succeeded")),
    libraryPlacement,
  } as unknown as LibraryApi;
  renderDialog(api, {onRefresh});

  fireEvent.click(await enabledApplyButton());
  expect(await screen.findByText(/Placement finished, but Library and Spark state could not be refreshed/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Retry Library refresh"}));

  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByText(/could not be refreshed/)).not.toBeInTheDocument());
  expect(libraryPlacement).not.toHaveBeenCalled();
});

test("announces zero-step completion without an invalid zero-range progressbar", async () => {
  const api = {
    previewLibraryPlacement: vi.fn(async () => placementPreview({steps: [{index: 0, kind: "keep", label: "Already installed", node_ids: [NODE_A, NODE_B]}]})),
    applyLibraryPlacement: vi.fn(async () => placementApplication("succeeded", {
      current_step: 0,
      total_steps: 0,
      progress: {completed_steps: 0, total_steps: 0},
      locations: {installation_ids: ["00000000-0000-4000-8000-000000000005"], run_ids: [], installed: true, running: false},
    })),
    libraryPlacement: vi.fn(),
  } as unknown as LibraryApi;
  renderDialog(api);

  fireEvent.click(await enabledApplyButton());
  const progress = await screen.findByRole("region", {name: "Placement progress"});
  expect(progress).toHaveAttribute("aria-live", "polite");
  expect(progress).toHaveTextContent("100%");
  expect(progress).toHaveTextContent("0 of 0 steps complete");
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
});

test("aborts preview, apply, progress, and terminal refresh requests on unmount", async () => {
  let previewSignal: AbortSignal | undefined;
  const previewApi = {
    previewLibraryPlacement: vi.fn((_input: LibraryPlacementPreviewInput, signal?: AbortSignal) => {
      previewSignal = signal;
      return new Promise<LibraryPlacementPreview>(() => undefined);
    }),
  } as unknown as LibraryApi;
  const previewRender = renderDialog(previewApi);
  await waitFor(() => expect(previewSignal).toBeInstanceOf(AbortSignal));
  previewRender.unmount();
  expect(previewSignal?.aborted).toBe(true);

  let applySignal: AbortSignal | undefined;
  const applyApi = {
    previewLibraryPlacement: vi.fn(async () => placementPreview()),
    applyLibraryPlacement: vi.fn((_input: LibraryPlacementApplyInput, signal?: AbortSignal) => {
      applySignal = signal;
      return new Promise<LibraryPlacementApplication>(() => undefined);
    }),
  } as unknown as LibraryApi;
  const applyRender = renderDialog(applyApi);
  fireEvent.click(await enabledApplyButton());
  await waitFor(() => expect(applySignal).toBeInstanceOf(AbortSignal));
  applyRender.unmount();
  expect(applySignal?.aborted).toBe(true);

  let progressSignal: AbortSignal | undefined;
  const progressApi = {
    previewLibraryPlacement: vi.fn(async () => placementPreview()),
    applyLibraryPlacement: vi.fn(async () => placementApplication("queued")),
    libraryPlacement: vi.fn((_id: string, signal?: AbortSignal) => {
      progressSignal = signal;
      return new Promise<LibraryPlacementApplication>(() => undefined);
    }),
  } as unknown as LibraryApi;
  const progressRender = renderDialog(progressApi);
  const progressApply = await enabledApplyButton();
  vi.useFakeTimers();
  fireEvent.click(progressApply);
  await flushPromises();
  await act(async () => { await vi.advanceTimersByTimeAsync(700); });
  expect(progressSignal).toBeInstanceOf(AbortSignal);
  progressRender.unmount();
  expect(progressSignal?.aborted).toBe(true);
  vi.useRealTimers();

  let refreshSignal: AbortSignal | undefined;
  const refreshApi = {
    previewLibraryPlacement: vi.fn(async () => placementPreview()),
    applyLibraryPlacement: vi.fn(async () => placementApplication("succeeded")),
    libraryPlacement: vi.fn(),
  } as unknown as LibraryApi;
  const refreshRender = renderDialog(refreshApi, {onRefresh: vi.fn((signal: AbortSignal) => {
    refreshSignal = signal;
    return new Promise<void>(() => undefined);
  })});
  fireEvent.click(await enabledApplyButton());
  await waitFor(() => expect(refreshSignal).toBeInstanceOf(AbortSignal));
  refreshRender.unmount();
  expect(refreshSignal?.aborted).toBe(true);
});

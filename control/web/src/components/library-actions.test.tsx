import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  ControlApi,
  LibraryInstallPlan,
  LibraryLoadPlan,
  LibraryMappingPlan,
  LibraryOperation,
  LibraryStopPlan,
  LibraryUninstallPlan,
} from "../api/types";
import {App} from "../app";
import {fullLibraryDetail, librarySnapshot} from "../test-fixtures/library";
import {LibraryOperationProgress} from "./library-operation-progress";

const GIB = 1024 ** 3;

function loadPlan(warnings: {code: string; detail: string}[] = []): LibraryLoadPlan {
  return {
    alias: "qwen-chat",
    allowed: true,
    installation_id: "installation-chat",
    mapping_generation: 4,
    mapping_id: "mapping-chat",
    nodes: [
      {active_reserved_bytes: 4 * GIB, allowed: true, available_memory_bytes: 100 * GIB, blockers: [], endpoint_owner: true, fabric_address: "fabric://node-alpha", fabric_bandwidth_mbps: 25_000, free_after_bytes: 36 * GIB, inventory_observed_at: "2026-08-15T11:59:50Z", memory_floor_bytes: 8 * GIB, memory_kind: "unified", node_id: "node-alpha", port: 8000, rank: 0, rendezvous_port: 29500, required_memory_bytes: 60 * GIB, role: "leader", warnings},
      {active_reserved_bytes: 4 * GIB, allowed: true, available_memory_bytes: 100 * GIB, blockers: [], endpoint_owner: false, fabric_address: "fabric://node-beta", fabric_bandwidth_mbps: 25_000, free_after_bytes: 36 * GIB, inventory_observed_at: "2026-08-15T11:59:45Z", memory_floor_bytes: 8 * GIB, memory_kind: "unified", node_id: "node-beta", port: 8000, rank: 1, rendezvous_port: null, required_memory_bytes: 60 * GIB, role: "worker", warnings: []},
    ],
    plan_digest: "load-plan-digest",
    recipe_revision_id: "revision-chat",
  };
}

function operation(state: string, result: Record<string, unknown> | null = null): LibraryOperation {
  return {id: "operation-load", kind: "run", owner_id: "installation-chat", state, plan_digest: "load-plan-digest", nodes: ["node-alpha", "node-beta"], result};
}

const mappingPlan: LibraryMappingPlan = {
  generation: 5,
  nodes: [{endpoint_owner: true, node_id: "node-alpha", rank: 0, role: "leader"}, {endpoint_owner: false, node_id: "node-beta", rank: 1, role: "worker"}],
  parameters: {tensor_parallel: 2}, placement_digest: "mapping-plan-digest", topology_name: "pair",
  recipe_content_sha256: "a".repeat(64), recipe_revision_id: "revision-chat",
};

const installPlan: LibraryInstallPlan = {
  allowed: true, image_digest: `sha256:${"d".repeat(64)}`, mapping_generation: 4, mapping_id: "mapping-chat",
  nodes: [
    {active_reserved_bytes: 5 * GIB, allowed: true, blockers: [], disk_floor_bytes: 20 * GIB, free_after_bytes: 135 * GIB, free_bytes: 200 * GIB, inventory_observed_at: "2026-08-15T11:59:50Z", node_id: "node-alpha", rank: 0, required_bytes: 60 * GIB, required_download_bytes: 40 * GIB, reused_bytes: 20 * GIB, role: "leader", warnings: []},
    {active_reserved_bytes: 5 * GIB, allowed: true, blockers: [], disk_floor_bytes: 20 * GIB, free_after_bytes: 135 * GIB, free_bytes: 200 * GIB, inventory_observed_at: "2026-08-15T11:50:00Z", node_id: "node-beta", rank: 1, required_bytes: 60 * GIB, required_download_bytes: 40 * GIB, reused_bytes: 20 * GIB, role: "worker", warnings: [{code: "inventory.stale", detail: "Admission inventory is stale for node-beta."}]},
  ],
  plan_digest: "install-plan-digest", recipe_build_id: "build-chat", recipe_content_sha256: "a".repeat(64), recipe_revision_id: "revision-chat",
};

function detailWithOperationalActions() {
  const detail = structuredClone(fullLibraryDetail);
  detail.operational_state.runs = [{run_id: "run-chat", installation_id: "installation-chat", mapping_id: "mapping-chat", recipe_revision_id: "revision-chat", node_ids: ["node-alpha", "node-beta"], route_state: "published", state: "running"}];
  return detail;
}

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("previews Load authority, applies its digest, and keeps partial grouped progress incomplete", async () => {
  // Break caught: Load applies without a digest-bound review, implies automatic
  // unload/coexistence, claims success optimistically, or loses the selected group.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const detail = structuredClone(fullLibraryDetail);
  const preview = loadPlan([{code: "run.coexistence_confirmed", detail: "Authoritative capacity evidence permits Qwen Code to coexist."}]);
  const previewLibraryLoad = vi.fn(async () => preview);
  const applyLibraryLoad = vi.fn(async () => operation("queued", {job_id: "job-load"}));
  const libraryOperation = vi.fn(async () => operation("partial", {job_id: "job-load"}));
  const retryLibraryOperation = vi.fn(async () => operation("queued", {job_id: "job-load"}));
  const libraryJobProgress = vi.fn(async () => ({
    id: "job-load", kind: "run", state: "failed", base_commit: "", current_attempt: 1,
    operation_total: 2, operations: [], progress: {completed: 1, failed: 1, running: 0, total: 2},
    target_total: 2, targets: ["node-alpha", "node-beta"],
  }));
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: vi.fn(async () => detail),
    previewLibraryLoad,
    applyLibraryLoad,
    libraryOperation,
    retryLibraryOperation,
    libraryJobProgress,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: "Select complete group node-alpha and node-beta"});
  await user.click(selector);
  const review = within(selector.closest("article")!).getByRole("button", {name: "Review Load"});
  await user.click(review);

  const dialog = await screen.findByRole("dialog", {name: "Review Load"});
  expect(previewLibraryLoad).toHaveBeenCalledWith({installation_id: "installation-chat", alias: "qwen-chat"}, expect.any(AbortSignal));
  expect(within(dialog).getByText("Endpoint alias qwen-chat")).toBeVisible();
  expect(within(dialog).getByText("Existing recipes remain loaded. Forge will not unload anything automatically.")).toBeVisible();
  expect(within(dialog).getByText("Authoritative capacity evidence permits Qwen Code to coexist.")).toBeVisible();
  expect(within(dialog).getByText("Rank 0 · leader · endpoint owner")).toBeVisible();
  expect(within(dialog).getAllByText("60.0 GiB required · 100.0 GiB available · 36.0 GiB after")).toHaveLength(2);

  await user.click(within(dialog).getByRole("button", {name: "Load selected installation"}));
  expect(applyLibraryLoad).toHaveBeenCalledWith({installation_id: "installation-chat", alias: "qwen-chat", plan_digest: "load-plan-digest", request_key: expect.any(String)}, expect.any(AbortSignal));
  const progress = await screen.findByRole("region", {name: "Load operation progress"});
  expect(within(progress).getByText("Operation incomplete")).toBeVisible();
  expect(within(progress).getByText("1 of 2 ranks completed · 1 failed")).toBeVisible();
  expect(within(progress).getByText("node-alpha + node-beta")).toBeVisible();
  expect(selector).toHaveAttribute("aria-pressed", "true");
  expect(within(selector.closest("article")!).getByRole("button", {name: "Review Load"})).toBeDisabled();

  await user.click(within(progress).getByRole("button", {name: "Retry incomplete operation"}));
  expect(retryLibraryOperation).toHaveBeenCalledWith("operation-load", expect.any(AbortSignal));
});

test("keeps a stale preview open and returns focus when a review sheet closes", async () => {
  // Break caught: stale authority closes the dialog, mutates optimistically, or
  // Escape loses keyboard focus instead of returning it to the invoking action.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const applyLibraryLoad = vi.fn(async (_input: unknown, _signal?: AbortSignal) => { throw new Error("preview_digest_stale: placement authority changed"); });
  const previewLibraryLoad = vi.fn(async () => loadPlan());
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    previewLibraryLoad,
    applyLibraryLoad,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  const review = within(selector.closest("article")!).getByRole("button", {name: "Review Load"});
  await user.click(review);
  const dialog = await screen.findByRole("dialog", {name: "Review Load"});
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", {name: "Review Load"})).not.toBeInTheDocument();
  expect(review).toHaveFocus();

  await user.click(review);
  const reopened = await screen.findByRole("dialog", {name: "Review Load"});
  expect(within(reopened).queryByText(/coexist/i)).not.toBeInTheDocument();
  await user.click(within(reopened).getByRole("button", {name: "Load selected installation"}));
  expect(await within(reopened).findByRole("alert")).toHaveTextContent("preview_digest_stale: placement authority changed");
  expect(screen.getByRole("dialog", {name: "Review Load"})).toBeVisible();
  expect(screen.queryByRole("region", {name: "Load operation progress"})).not.toBeInTheDocument();
  expect(within(reopened).getByRole("button", {name: "Load selected installation"})).toBeDisabled();
  await user.click(within(reopened).getByRole("button", {name: "Review fresh preview"}));
  expect(previewLibraryLoad).toHaveBeenCalledTimes(3);
  expect(within(reopened).getByRole("button", {name: "Load selected installation"})).toBeEnabled();
  const staleRequestKey = (applyLibraryLoad.mock.calls[0][0] as {request_key: string}).request_key;
  await user.click(within(reopened).getByRole("button", {name: "Load selected installation"}));
  const freshRequestKey = (applyLibraryLoad.mock.calls[1][0] as {request_key: string}).request_key;
  expect(freshRequestKey).not.toBe(staleRequestKey);
});

test("applies Mapping and Install only from their distinct authoritative previews", async () => {
  // Break caught: placement actions share a generic endpoint/body, skip the
  // reviewed digest, or let Install omit its exact build/mapping authority.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const detail = structuredClone(fullLibraryDetail);
  const lateInstallPlan = structuredClone(installPlan);
  lateInstallPlan.nodes[0].inventory_observed_at = "2026-08-15T12:09:50Z";
  lateInstallPlan.nodes[0].warnings = [{code: "inventory.stale", detail: "Server preview classified node-alpha inventory as stale."}];
  lateInstallPlan.nodes[1].inventory_observed_at = "2026-08-15T12:09:40Z";
  lateInstallPlan.nodes[1].warnings = [{code: "inventory.unavailable", detail: "Server preview could not authorize node-beta inventory."}];
  lateInstallPlan.nodes.push({...lateInstallPlan.nodes[1], inventory_observed_at: "2026-08-15T12:01:00Z", node_id: "node-gamma", rank: 2, warnings: []});
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-08-15T12:10:00Z"));
  detail.placement[0].recommendations[0].preview_targets = [
    {kind: "mapping", input: {recipe_revision_id: "revision-chat", node_ids: ["node-alpha", "node-beta"], parameters: {tensor_parallel: 2}}},
    {kind: "install", input: {recipe_build_id: "build-chat", mapping_id: "mapping-chat"}},
    {kind: "run", input: {installation_id: "installation-chat"}},
  ];
  const applyLibraryMapping = vi.fn(async () => ({mapping_id: "mapping-chat", generation: 5, placement_digest: "mapping-plan-digest"}));
  const applyLibraryInstall = vi.fn(async () => ({...operation("succeeded"), id: "operation-install", kind: "install", owner_id: "installation-chat", plan_digest: "install-plan-digest"}));
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: vi.fn(async () => detail),
    previewLibraryMapping: vi.fn(async () => mappingPlan), applyLibraryMapping,
    previewLibraryInstall: vi.fn(async () => lateInstallPlan), applyLibraryInstall,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  const group = selector.closest("article")!;

  await user.click(within(group).getByRole("button", {name: "Review Mapping"}));
  const mapping = await screen.findByRole("dialog", {name: "Review Mapping"});
  expect(within(mapping).getByText("Rank 0 · leader · endpoint owner · node-alpha")).toBeVisible();
  expect(within(mapping).getAllByText("60.0 GiB disk required · 5.0 GiB reserved · 135.0 GiB after")).toHaveLength(2);
  expect(within(mapping).getAllByText("20.0 GiB exact artifacts reused")).toHaveLength(2);
  expect(within(mapping).getByText("Inventory fresh · 10s")).toBeVisible();
  expect(within(mapping).getByText("placement.artifact_reuse")).toBeVisible();
  await user.click(within(mapping).getByRole("button", {name: "Create selected mapping"}));
  expect(applyLibraryMapping).toHaveBeenCalledWith({...detail.placement[0].recommendations[0].preview_targets[0].input, placement_digest: "mapping-plan-digest", request_key: expect.any(String)}, expect.any(AbortSignal));
  await waitFor(() => expect(screen.queryByRole("dialog", {name: "Review Mapping"})).not.toBeInTheDocument());

  await user.click(within(group).getByRole("button", {name: "Review Install"}));
  const install = await screen.findByRole("dialog", {name: "Review Install"});
  expect(within(install).getAllByText("60.0 GiB disk required · 40.0 GiB download · 20.0 GiB reused")).toHaveLength(3);
  expect(within(install).getByText("Inventory stale · 10s")).toBeVisible();
  expect(within(install).getByText("Inventory stale · 540s")).toBeVisible();
  expect(within(install).getByText("Inventory unavailable · server preview evidence")).toBeVisible();
  expect(within(install).queryByText(/^Inventory fresh/)).not.toBeInTheDocument();
  expect(within(install).getByText("inventory.stale")).toBeVisible();
  expect(within(install).getByText("inventory.unavailable")).toBeVisible();
  expect(within(install).getByText("Server preview classified node-alpha inventory as stale.")).toBeVisible();
  expect(within(install).getByText("Server preview could not authorize node-beta inventory.")).toBeVisible();
  await user.click(within(install).getByRole("button", {name: "Install on selected nodes"}));
  expect(applyLibraryInstall).toHaveBeenCalledWith({recipe_build_id: "build-chat", mapping_id: "mapping-chat", plan_digest: "install-plan-digest", request_key: expect.any(String)}, expect.any(AbortSignal));
  expect(await screen.findByRole("region", {name: "Install operation progress"})).toHaveTextContent("Operation complete");
  expect(selector).toHaveAttribute("aria-pressed", "true");
});

test("reuses one apply request key for ambiguous retries of the reviewed preview", async () => {
  // Break caught: retrying an apply after an ambiguous response invents a new
  // idempotency key and can duplicate a mutation the server already accepted.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const applyLibraryLoad = vi.fn()
    .mockRejectedValueOnce(new Error("network response was lost"))
    .mockResolvedValueOnce(operation("succeeded"));
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    previewLibraryLoad: async () => loadPlan(),
    applyLibraryLoad,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  await user.click(within(selector.closest("article")!).getByRole("button", {name: "Review Load"}));
  const dialog = await screen.findByRole("dialog", {name: "Review Load"});
  const applyButton = within(dialog).getByRole("button", {name: "Load selected installation"});
  await user.click(applyButton);
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("network response was lost");
  await user.click(applyButton);

  await waitFor(() => expect(applyLibraryLoad).toHaveBeenCalledTimes(2));
  const first = applyLibraryLoad.mock.calls[0][0] as {request_key?: string};
  const second = applyLibraryLoad.mock.calls[1][0] as {request_key?: string};
  expect(first.request_key).toMatch(/^[0-9a-f-]{36}$/);
  expect(second.request_key).toBe(first.request_key);
});

test("publishes deferred job progress and refreshes terminal authority before changing operation state", async () => {
  // Break caught: publishing terminal operation state first tears down the poll
  // effect and loses both deferred grouped progress and the terminal refetch.
  let resolveJob!: (value: Awaited<ReturnType<ControlApi["libraryJobProgress"]>>) => void;
  const jobPromise = new Promise<Awaited<ReturnType<ControlApi["libraryJobProgress"]>>>(resolve => { resolveJob = resolve; });
  const terminal = operation("partial", {job_id: "job-load"});
  const onChange = vi.fn();
  const onRefresh = vi.fn(async () => undefined);
  const api = {
    libraryOperation: vi.fn(async () => terminal),
    libraryJobProgress: vi.fn(() => jobPromise),
  } as unknown as ControlApi;
  render(<LibraryOperationProgress api={api} name="Load" operation={operation("running")} onChange={onChange} onRefresh={onRefresh}/>);

  await waitFor(() => expect(api.libraryJobProgress).toHaveBeenCalledWith("job-load", expect.any(AbortSignal)));
  expect(onChange).not.toHaveBeenCalled();
  expect(onRefresh).not.toHaveBeenCalled();
  resolveJob({
    id: "job-load", kind: "run", state: "failed", base_commit: "", current_attempt: 1,
    operation_total: 2, operations: [], progress: {completed: 1, failed: 1, running: 0, total: 2},
    target_total: 2, targets: ["node-alpha", "node-beta"],
  });

  expect(await screen.findByText("1 of 2 ranks completed · 1 failed")).toBeVisible();
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(onChange).toHaveBeenCalledWith(terminal);
  expect(onRefresh.mock.invocationCallOrder[0]).toBeLessThan(onChange.mock.invocationCallOrder[0]);
});

test("aborts in-flight preview, apply, operation, and job requests on cleanup", async () => {
  // Break caught: navigating away merely ignores responses while authority
  // requests remain alive and imperative completions still update unmounted UI.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  let previewSignal: AbortSignal | undefined;
  const previewLibraryLoad = vi.fn((_input: unknown, signal?: AbortSignal) => {
    previewSignal = signal;
    return new Promise<LibraryLoadPlan>(() => undefined);
  });
  const previewApi = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    previewLibraryLoad,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  const previewRender = render(<App api={previewApi}/>);
  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  await user.click(within(selector.closest("article")!).getByRole("button", {name: "Review Load"}));
  await waitFor(() => expect(previewSignal).toBeInstanceOf(AbortSignal));
  previewRender.unmount();
  expect(previewSignal?.aborted).toBe(true);

  let applySignal: AbortSignal | undefined;
  const applyLibraryLoad = vi.fn((_input: unknown, signal?: AbortSignal) => {
    applySignal = signal;
    return new Promise<LibraryOperation>(() => undefined);
  });
  const applyApi = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    previewLibraryLoad: async () => loadPlan(),
    applyLibraryLoad,
  } as unknown as ControlApi;
  const applyRender = render(<App api={applyApi}/>);
  const applyPlacement = await screen.findByRole("region", {name: "Complete placement groups"});
  const applySelector = within(applyPlacement).getByRole("button", {name: /Select complete group/});
  await user.click(applySelector);
  await user.click(within(applySelector.closest("article")!).getByRole("button", {name: "Review Load"}));
  const applyDialog = await screen.findByRole("dialog", {name: "Review Load"});
  await user.click(within(applyDialog).getByRole("button", {name: "Load selected installation"}));
  await waitFor(() => expect(applySignal).toBeInstanceOf(AbortSignal));
  applyRender.unmount();
  expect(applySignal?.aborted).toBe(true);

  let operationSignal: AbortSignal | undefined;
  const pollApi = {
    libraryOperation: vi.fn((_id: string, signal?: AbortSignal) => {
      operationSignal = signal;
      return new Promise<LibraryOperation>(() => undefined);
    }),
  } as unknown as ControlApi;
  const pollRender = render(<LibraryOperationProgress api={pollApi} name="Load" operation={operation("running")} onChange={() => undefined} onRefresh={async () => undefined}/>);
  await waitFor(() => expect(operationSignal).toBeInstanceOf(AbortSignal));
  pollRender.unmount();
  expect(operationSignal?.aborted).toBe(true);

  let jobSignal: AbortSignal | undefined;
  const jobApi = {
    libraryOperation: vi.fn(async () => operation("running", {job_id: "job-load"})),
    libraryJobProgress: vi.fn((_id: string, signal?: AbortSignal) => {
      jobSignal = signal;
      return new Promise<Awaited<ReturnType<ControlApi["libraryJobProgress"]>>>(() => undefined);
    }),
  } as unknown as ControlApi;
  const jobRender = render(<LibraryOperationProgress api={jobApi} name="Load" operation={operation("queued")} onChange={() => undefined} onRefresh={async () => undefined}/>);
  await waitFor(() => expect(jobSignal).toBeInstanceOf(AbortSignal));
  jobRender.unmount();
  expect(jobSignal?.aborted).toBe(true);

  let retrySignal: AbortSignal | undefined;
  const retryApi = {
    retryLibraryOperation: vi.fn((_id: string, signal?: AbortSignal) => {
      retrySignal = signal;
      return new Promise<LibraryOperation>(() => undefined);
    }),
  } as unknown as ControlApi;
  const retryRender = render(<LibraryOperationProgress api={retryApi} name="Load" operation={operation("partial")} onChange={() => undefined} onRefresh={async () => undefined}/>);
  await user.click(screen.getByRole("button", {name: "Retry incomplete operation"}));
  await waitFor(() => expect(retrySignal).toBeInstanceOf(AbortSignal));
  retryRender.unmount();
  expect(retrySignal?.aborted).toBe(true);
});

test("aborts an apply-owned detail refresh and ignores its late authority response", async () => {
  // Break caught: closing an applying review aborts the mutation but leaves its
  // signal-less detail refresh alive to overwrite the still-mounted page.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  let refreshSignal: AbortSignal | undefined;
  let resolveRefresh!: (value: typeof fullLibraryDetail) => void;
  const refresh = new Promise<typeof fullLibraryDetail>(resolve => { resolveRefresh = resolve; });
  const libraryRecipe = vi.fn((_recipeId: string, signal?: AbortSignal) => {
    if (libraryRecipe.mock.calls.length === 1) return Promise.resolve(fullLibraryDetail);
    refreshSignal = signal;
    return refresh;
  });
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe,
    previewLibraryLoad: async () => loadPlan(),
    applyLibraryLoad: async () => operation("queued"),
    libraryOperation: vi.fn(async () => operation("running")),
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  await user.click(within(selector.closest("article")!).getByRole("button", {name: "Review Load"}));
  const dialog = await screen.findByRole("dialog", {name: "Review Load"});
  await user.click(within(dialog).getByRole("button", {name: "Load selected installation"}));
  await waitFor(() => expect(libraryRecipe).toHaveBeenCalledTimes(2));
  await user.click(within(dialog).getByRole("button", {name: "Close review"}));

  expect(refreshSignal).toBeInstanceOf(AbortSignal);
  expect(refreshSignal?.aborted).toBe(true);
  const lateDetail = structuredClone(fullLibraryDetail);
  lateDetail.recipe.title = "Late refresh must be ignored";
  resolveRefresh(lateDetail);
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByText("Late refresh must be ignored")).not.toBeInTheDocument();
  expect(screen.queryByRole("region", {name: "Load operation progress"})).not.toBeInTheDocument();
});

test("aborts a terminal-poll detail refresh and suppresses its late state callback", async () => {
  // Break caught: terminal operation cleanup aborts polling but not the detail
  // refresh awaited before publishing terminal operation state.
  let refreshSignal: AbortSignal | undefined;
  let resolveRefresh!: () => void;
  const refresh = new Promise<void>(resolve => { resolveRefresh = resolve; });
  const onRefresh = vi.fn((signal?: AbortSignal) => {
    refreshSignal = signal;
    return refresh;
  });
  const onChange = vi.fn();
  const api = {libraryOperation: vi.fn(async () => operation("succeeded"))} as unknown as ControlApi;
  const rendered = render(<LibraryOperationProgress api={api} name="Load" operation={operation("running")} onChange={onChange} onRefresh={onRefresh}/>);

  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  rendered.unmount();
  expect(refreshSignal).toBeInstanceOf(AbortSignal);
  expect(refreshSignal?.aborted).toBe(true);
  resolveRefresh();
  await act(async () => { await Promise.resolve(); });
  expect(onChange).not.toHaveBeenCalled();
});

test("retries a failed preview without closing its review", async () => {
  // Break caught: a transient preview error strands the user or applies without
  // obtaining a fresh server plan in the same dialog.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const previewLibraryLoad = vi.fn()
    .mockRejectedValueOnce(new Error("admission inventory temporarily unavailable"))
    .mockResolvedValueOnce(loadPlan());
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    previewLibraryLoad,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  await user.click(within(selector.closest("article")!).getByRole("button", {name: "Review Load"}));
  const dialog = await screen.findByRole("dialog", {name: "Review Load"});
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("admission inventory temporarily unavailable");
  await user.click(within(dialog).getByRole("button", {name: "Retry preview"}));
  expect(await within(dialog).findByText("Existing recipes remain loaded. Forge will not unload anything automatically.")).toBeVisible();
  expect(previewLibraryLoad).toHaveBeenCalledTimes(2);
});

test("previews Stop and Remove consequences without implying released capacity or catalog deletion", async () => {
  // Break caught: Stop targets less than a complete rank group, or Remove hides
  // active-run blockers/unknown bytes and suggests it deletes the recipe.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const stopPlan: LibraryStopPlan = {
    alias: "qwen-chat", allowed: true, authority_digest: "run-authority", blockers: [], installation_id: "installation-chat",
    nodes: [{active_memory_reservation_bytes: 4 * GIB, node_id: "node-alpha", rank: 0, reserved_memory_bytes: 60 * GIB, role: "leader", state: "running"}, {active_memory_reservation_bytes: 4 * GIB, node_id: "node-beta", rank: 1, reserved_memory_bytes: 60 * GIB, role: "worker", state: "running"}],
    plan_digest: "stop-plan", recipe_revision_id: "revision-chat", route_digest: "route-digest", route_generation: 8,
    route_state: "published", route_withdrawal: true, run_id: "run-chat", run_state: "running", total_active_memory_reservation_bytes: 8 * GIB, warnings: [],
  };
  const uninstallPlan: LibraryUninstallPlan = {
    active_run_count: 1, active_runs: [{alias: "qwen-chat", route_state: "published", run_id: "run-chat", state: "running"}], active_runs_truncated: false,
    allowed: false, blockers: [{code: "uninstall.active_runs", detail: "Stop the complete active run before removing this installation."}, {code: "uninstall.bytes_unknown", detail: "Exact removable bytes are unknown."}], bytes_removed: null,
    consequences: {automatic_stop: false, catalog_retained: true, reinstall_required: true}, installation_authority_digest: "install-authority", installation_id: "installation-chat", installation_state: "installed",
    nodes: [{installed_bytes: null, node_id: "node-alpha", rank: 0, role: "leader", state: "installed"}, {installed_bytes: 60 * GIB, node_id: "node-beta", rank: 1, role: "worker", state: "installed"}],
    original_plan_digest: "install-plan", plan_digest: "uninstall-plan", recipe_content: {}, recipe_content_sha256: "a".repeat(64), recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", warnings: [],
  };
  const allowedUninstallPlan = {...uninstallPlan, active_run_count: 0, active_runs: [], allowed: true, blockers: [], bytes_removed: 120 * GIB};
  const previewLibraryUninstall = vi.fn().mockResolvedValueOnce(uninstallPlan).mockResolvedValueOnce(allowedUninstallPlan);
  const applyLibraryStop = vi.fn(async () => ({...operation("succeeded"), id: "operation-stop", kind: "stop", owner_id: "run-chat", plan_digest: "stop-plan"}));
  const applyLibraryUninstall = vi.fn(async () => ({...operation("succeeded"), id: "operation-remove", kind: "uninstall", owner_id: "installation-chat", plan_digest: "uninstall-plan"}));
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => detailWithOperationalActions(),
    previewLibraryStop: vi.fn(async () => stopPlan),
    applyLibraryStop,
    previewLibraryUninstall,
    applyLibraryUninstall,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  await user.click(await screen.findByRole("button", {name: "Review Stop run run-chat"}));
  const stop = await screen.findByRole("dialog", {name: "Review Stop"});
  expect(within(stop).getByText("Published route will be withdrawn.")).toBeVisible();
  expect(within(stop).getByText("Rank 0 · leader · running")).toBeVisible();
  expect(within(stop).getByText("Rank 1 · worker · running")).toBeVisible();
  expect(within(stop).getByText("Capacity remains reserved unless every rank stops successfully.")).toBeVisible();
  await user.click(within(stop).getByRole("button", {name: "Stop selected run"}));
  expect(applyLibraryStop).toHaveBeenCalledWith("run-chat", {plan_digest: "stop-plan", request_key: expect.any(String)}, expect.any(AbortSignal));
  expect(await screen.findByRole("region", {name: "Stop operation progress"})).toHaveTextContent("Operation complete");

  await user.click(screen.getByRole("button", {name: "Review Remove installation installation-chat"}));
  const remove = await screen.findByRole("dialog", {name: "Review Remove"});
  expect(within(remove).getAllByText("Exact removable bytes are unknown.")).toHaveLength(2);
  expect(within(remove).getByText("uninstall.bytes_unknown")).toBeVisible();
  expect(within(remove).getByText("Forge will not stop active runs automatically.")).toBeVisible();
  expect(within(remove).getByText("The local catalog recipe is retained.")).toBeVisible();
  expect(within(remove).getByText("Reinstall is required to restore removed content.")).toBeVisible();
  expect(within(remove).getByRole("button", {name: "Remove selected installation"})).toBeDisabled();
  await user.click(within(remove).getByRole("button", {name: "Close review"}));

  await user.click(screen.getByRole("button", {name: "Review Remove installation installation-chat"}));
  const allowedRemove = await screen.findByRole("dialog", {name: "Review Remove"});
  expect(within(allowedRemove).getByText("120.0 GiB will be removed.")).toBeVisible();
  await user.click(within(allowedRemove).getByRole("button", {name: "Remove selected installation"}));
  expect(applyLibraryUninstall).toHaveBeenCalledWith("installation-chat", {plan_digest: "uninstall-plan", request_key: expect.any(String)}, expect.any(AbortSignal));
  expect(await screen.findByRole("region", {name: "Remove operation progress"})).toHaveTextContent("Operation complete");
});

test("retries operation status errors and cancels pending polls on cleanup", async () => {
  // Break caught: a polling error is terminal, or a Library unmount leaves a
  // timer that continues querying operation authority after cleanup.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const libraryOperation = vi.fn()
    .mockRejectedValueOnce(new Error("operation authority temporarily unavailable"))
    .mockResolvedValue(operation("running"));
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    previewLibraryLoad: async () => loadPlan(),
    applyLibraryLoad: async () => operation("queued"),
    libraryOperation,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  const rendered = render(<App api={api}/>);

  const placement = await screen.findByRole("region", {name: "Complete placement groups"});
  const selector = within(placement).getByRole("button", {name: /Select complete group/});
  await user.click(selector);
  await user.click(within(selector.closest("article")!).getByRole("button", {name: "Review Load"}));
  const dialog = await screen.findByRole("dialog", {name: "Review Load"});
  await user.click(within(dialog).getByRole("button", {name: "Load selected installation"}));
  const progress = await screen.findByRole("region", {name: "Load operation progress"});
  expect(await within(progress).findByRole("alert")).toHaveTextContent("operation authority temporarily unavailable");

  await user.click(within(progress).getByRole("button", {name: "Retry status"}));
  await waitFor(() => expect(libraryOperation.mock.calls.length).toBeGreaterThanOrEqual(2));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)); });
  const callsBeforeUnmount = libraryOperation.mock.calls.length;
  rendered.unmount();
  await new Promise(resolve => setTimeout(resolve, 1100));
  expect(libraryOperation).toHaveBeenCalledTimes(callsBeforeUnmount);
});

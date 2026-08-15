import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {LibraryApi} from "../api/types";
import {fullLibraryDetail, librarySnapshot} from "../test-fixtures/library";
import {LibraryRecipeAuthority} from "./library-recipe-detail";

function renderAuthority() {
  render(<LibraryRecipeAuthority
    api={{} as LibraryApi}
    detail={fullLibraryDetail}
    onRefresh={async () => undefined}
    policy={librarySnapshot.freshness_policy}
  />);
}

test("keeps raw JSON progressive and preserves the last valid visual preview on editor errors", async () => {
  // Break caught: Advanced replaces the visual view, invalid JSON blanks or
  // corrupts that view, errors lack a field path, or local edits imply apply.
  renderAuthority();
  const user = userEvent.setup();
  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  const summary = within(advanced).getByText("Advanced recipe document");
  expect(advanced).not.toHaveAttribute("open");
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/3");

  await user.click(summary);
  expect(advanced).toHaveAttribute("open");
  const editor = within(advanced).getByRole("textbox", {name: "Recipe JSON"});
  editor.focus();
  const changed = {
    ...fullLibraryDetail.visual_recipe!,
    workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/preview"},
    runtime: {...fullLibraryDetail.visual_recipe!.runtime, adapter: "sglang", endpoint_port: 9000},
  };
  fireEvent.change(editor, {target: {value: JSON.stringify(changed, null, 2)}});
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/preview");
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("sglang v1");

  fireEvent.change(editor, {target: {value: JSON.stringify({...changed, workload: {capabilities: [], family: 4}}, null, 2)}});
  const error = within(advanced).getByRole("alert");
  expect(error).toHaveTextContent("$.workload.family must be a string.");
  expect(editor).toHaveAttribute("aria-invalid", "true");
  expect(editor).toHaveAttribute("aria-describedby", error.id);
  expect(editor).toHaveFocus();
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/preview");
  expect(screen.queryByRole("button", {name: /apply|publish/i})).not.toBeInTheDocument();

  expect(screen.getByRole("link", {name: "Source and build"})).toHaveAttribute("href", "/catalog/recipe-chat/source");
  expect(screen.getByRole("link", {name: "Cluster mapping"})).toHaveAttribute("href", "/catalog/recipe-chat/map");
  expect(screen.getByRole("link", {name: "Raw editor"})).toHaveAttribute("href", "/catalog/recipe-chat");
});

test("associates upload errors with the file control and recovers with a valid file without stealing focus", async () => {
  // Break caught: upload failures are detached from the input, clear the last
  // valid preview, steal focus, or prevent a corrected file from being read.
  renderAuthority();
  const user = userEvent.setup();
  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  await user.click(within(advanced).getByText("Advanced recipe document"));
  const upload = within(advanced).getByLabelText("Upload recipe JSON");

  await user.upload(upload, new File([JSON.stringify({...fullLibraryDetail.visual_recipe, metadata: {title: 7}})], "invalid.json", {type: "application/json"}));
  const error = await within(advanced).findByRole("alert");
  expect(error).toHaveTextContent("$.metadata.title must be a string.");
  expect(upload).toHaveAttribute("aria-invalid", "true");
  expect(upload).toHaveAttribute("aria-describedby", error.id);
  expect(upload).toHaveFocus();
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/3");

  const changed = {...fullLibraryDetail.visual_recipe!, workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/uploaded"}};
  await user.upload(upload, new File([JSON.stringify(changed)], "valid.json", {type: "application/json"}));
  expect(within(advanced).queryByRole("alert")).not.toBeInTheDocument();
  expect(upload).toHaveAttribute("aria-invalid", "false");
  expect(upload).toHaveFocus();
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/uploaded");
});

test("never lets a local Advanced alias edit change server action authority", async () => {
  // Break caught: preview-only JSON leaks into Load authority and changes the
  // alias sent to the server preview without an explicit catalog workflow.
  const previewLibraryLoad = vi.fn(() => new Promise<never>(() => undefined));
  render(<LibraryRecipeAuthority
    api={{previewLibraryLoad} as unknown as LibraryApi}
    detail={fullLibraryDetail}
    onRefresh={async () => undefined}
    policy={librarySnapshot.freshness_policy}
  />);
  const user = userEvent.setup();
  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  await user.click(within(advanced).getByText("Advanced recipe document"));
  const local = {
    ...fullLibraryDetail.visual_recipe!,
    runtime: {...fullLibraryDetail.visual_recipe!.runtime, model_aliases: ["local-preview-only"]},
  };
  fireEvent.change(within(advanced).getByRole("textbox", {name: "Recipe JSON"}), {target: {value: JSON.stringify(local)}});

  await user.click(screen.getByRole("button", {name: "Select complete group node-alpha and node-beta"}));
  await user.click(screen.getByRole("button", {name: "Review Load"}));
  await waitFor(() => expect(previewLibraryLoad).toHaveBeenCalledWith(
    {installation_id: "installation-chat", alias: "qwen-chat"},
    expect.any(AbortSignal),
  ));
});

test("renders every canonical visual section from the valid local preview and labels it as unsaved", async () => {
  // Break caught: only workload/runtime summary fields react to a local edit,
  // leaving canonical hero/build/artifact fields beside a supposedly valid preview.
  renderAuthority();
  const user = userEvent.setup();
  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  await user.click(within(advanced).getByText("Advanced recipe document"));
  const local = {
    ...fullLibraryDetail.visual_recipe!,
    identity: {publisher: "preview-publisher", slug: "preview-slug"},
    metadata: {title: "Preview title", description: "Preview description", tags: ["preview-tag"]},
    workload: {family: "preview/family", capabilities: ["preview.capability"]},
    build: {
      context: {sha256: "e".repeat(64), expected_bytes: 1234, media_type: "application/preview+tar"},
      dockerfile: "Preview.Dockerfile",
      platform: "linux/amd64",
      network_mode: "allowlist",
      network_hosts: ["preview.example"],
      download_bytes: 111,
      temporary_bytes: 222,
      memory_bytes: 333,
      timeout_seconds: 44,
    },
    artifacts: [{
      id: "preview-artifact",
      kind: "preview.kind",
      repository: "preview/repository",
      revision: "preview-revision",
      download_bytes: 444,
      installed_bytes: 555,
      roles: ["preview-role"],
    }],
    runtime: {
      interface: "preview.interface.v1",
      adapter: "preview-adapter",
      adapter_version: 2,
      endpoint_protocol: "grpc",
      endpoint_port: 9001,
      model_aliases: ["preview-alias", "preview-backup"],
      health_path: "/preview-ready",
    },
    provenance: {source_kind: "fork" as const, source_reference: "preview-source", attribution: ["Preview Author"]},
    validation: {checks: ["preview.check"], benchmark_count: 7},
  };

  fireEvent.change(within(advanced).getByRole("textbox", {name: "Recipe JSON"}), {target: {value: JSON.stringify(local)}});

  const authority = screen.getByRole("region", {name: "Qwen Chat recipe authority"});
  expect(within(authority).getByText("Local preview · not saved")).toBeVisible();
  expect(within(authority).getByText("preview-publisher/preview-slug")).toBeVisible();
  expect(within(authority).getByText("Preview title")).toBeVisible();
  expect(within(authority).getByText("Preview description")).toBeVisible();
  expect(within(authority).getByText("preview-tag")).toBeVisible();

  const model = within(authority).getByRole("region", {name: "Model and runtime"});
  expect(model).toHaveTextContent("preview/family");
  expect(model).toHaveTextContent("preview.capability");

  const build = within(authority).getByRole("region", {name: "Build and artifacts"});
  for (const value of [
    "Schema version 1", "Preview.Dockerfile", "linux/amd64", "allowlist", "preview.example",
    "1,234 bytes", "application/preview+tar", `sha256:${"e".repeat(64)}`,
    "111 bytes", "222 bytes", "333 bytes", "44 seconds",
    "preview-artifact", "preview.kind", "preview/repository", "preview-revision",
    "444 bytes", "555 bytes", "preview-role",
  ]) expect(build).toHaveTextContent(value);

  const runtime = within(authority).getByRole("region", {name: "Runtime contract"});
  for (const value of ["preview.interface.v1", "preview-adapter", "Version 2", "grpc", "Port 9,001", "preview-alias", "preview-backup", "/preview-ready"])
    expect(runtime).toHaveTextContent(value);

  const evidence = within(authority).getByRole("region", {name: "Provenance and validation"});
  for (const value of ["fork", "preview-source", "Preview Author", "preview.check", "7 benchmarks"])
    expect(evidence).toHaveTextContent(value);
});

test("tracks local preview origin across equivalent canonical refreshes", () => {
  // Break caught: a newly deserialized but content-equivalent server document
  // was mistaken for an unsaved edit because dirty state compared object identity.
  const props = {
    api: {} as LibraryApi,
    onRefresh: async () => undefined,
    policy: librarySnapshot.freshness_policy,
  };
  const equivalentDetail = {
    ...fullLibraryDetail,
    visual_recipe: JSON.parse(JSON.stringify(fullLibraryDetail.visual_recipe)),
  };
  const view = render(<LibraryRecipeAuthority {...props} detail={fullLibraryDetail}/>);

  view.rerender(<LibraryRecipeAuthority {...props} detail={equivalentDetail}/>);
  expect(screen.queryByText("Local preview · not saved")).not.toBeInTheDocument();

  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  fireEvent.click(within(advanced).getByText("Advanced recipe document"));
  const local = {
    ...fullLibraryDetail.visual_recipe!,
    workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/local-equivalent-refresh"},
  };
  fireEvent.change(within(advanced).getByRole("textbox", {name: "Recipe JSON"}), {
    target: {value: JSON.stringify(local)},
  });
  expect(screen.getByText("Local preview · not saved")).toBeVisible();

  view.rerender(<LibraryRecipeAuthority
    {...props}
    detail={{...fullLibraryDetail, visual_recipe: JSON.parse(JSON.stringify(fullLibraryDetail.visual_recipe))}}
  />);
  expect(screen.getByText("Local preview · not saved")).toBeVisible();
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/local-equivalent-refresh");
});

test("resets canonical preview state without remounting focused controls and ignores polling-only refresh", async () => {
  // Break caught: using a React key to reset Advanced destroys the focused
  // editor/upload, while failing to reset can resurrect stale A to B to A state.
  const props = {
    api: {} as LibraryApi,
    onRefresh: async () => undefined,
    policy: librarySnapshot.freshness_policy,
  };
  const view = render(<LibraryRecipeAuthority {...props} detail={fullLibraryDetail}/>);
  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  fireEvent.click(within(advanced).getByText("Advanced recipe document"));
  const editor = within(advanced).getByRole("textbox", {name: "Recipe JSON"});
  const local = {
    ...fullLibraryDetail.visual_recipe!,
    workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/local-preview"},
  };
  fireEvent.change(editor, {target: {value: JSON.stringify(local)}});
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/local-preview");
  fireEvent.change(editor, {target: {value: JSON.stringify({...local, workload: {...local.workload, family: 4}})}});
  expect(within(advanced).getByRole("alert")).toHaveTextContent("$.workload.family must be a string.");
  editor.focus();

  const refreshedVisual = {
    ...fullLibraryDetail.visual_recipe!,
    workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/server-refresh"},
  };
  const refreshedDetail = {...fullLibraryDetail, visual_recipe: refreshedVisual};
  view.rerender(<LibraryRecipeAuthority {...props} detail={refreshedDetail}/>);
  await waitFor(() => expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/server-refresh"));
  let nextAdvanced = screen.getByRole("group", {name: "Advanced recipe document"});
  expect(nextAdvanced).toHaveAttribute("open");
  expect(within(nextAdvanced).getByRole("textbox", {name: "Recipe JSON"})).toBe(editor);
  expect(editor).toHaveValue(JSON.stringify(refreshedVisual, null, 2));
  expect(within(nextAdvanced).queryByRole("alert")).not.toBeInTheDocument();
  expect(editor).toHaveFocus();

  const upload = within(nextAdvanced).getByLabelText("Upload recipe JSON");
  upload.focus();
  const secondRefreshedVisual = {
    ...refreshedVisual,
    metadata: {...refreshedVisual.metadata, title: "Qwen Server Refresh Two"},
  };
  const secondRefreshedDetail = {...refreshedDetail, visual_recipe: secondRefreshedVisual};
  view.rerender(<LibraryRecipeAuthority {...props} detail={secondRefreshedDetail}/>);
  await waitFor(() => expect(editor).toHaveValue(JSON.stringify(secondRefreshedVisual, null, 2)));
  expect(within(nextAdvanced).getByLabelText("Upload recipe JSON")).toBe(upload);
  expect(upload).toHaveFocus();

  const pollingDraft = {...secondRefreshedVisual, workload: {...secondRefreshedVisual.workload, family: "qwen/polling-draft"}};
  fireEvent.change(editor, {target: {value: JSON.stringify(pollingDraft)}});
  upload.focus();
  view.rerender(<LibraryRecipeAuthority
    {...props}
    detail={{...secondRefreshedDetail, operational_state: {...secondRefreshedDetail.operational_state, runs: []}}}
  />);
  expect(editor).toHaveValue(JSON.stringify(pollingDraft));
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/polling-draft");
  expect(upload).toHaveFocus();

  const recipeBVisual = {
    ...fullLibraryDetail.visual_recipe!,
    identity: {...fullLibraryDetail.visual_recipe!.identity, slug: "qwen-code"},
    workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/code"},
  };
  const recipeBDetail = {
    ...fullLibraryDetail,
    recipe: {...fullLibraryDetail.recipe, recipe_id: "recipe-code", slug: "qwen-code", title: "Qwen Code"},
    visual_recipe: recipeBVisual,
  };
  view.rerender(<LibraryRecipeAuthority {...props} detail={recipeBDetail}/>);
  await waitFor(() => expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/code"));
  view.rerender(<LibraryRecipeAuthority {...props} detail={fullLibraryDetail}/>);
  await waitFor(() => expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/3"));
  expect(screen.getByRole("region", {name: "Model and runtime"})).not.toHaveTextContent("qwen/local-preview");
  nextAdvanced = screen.getByRole("group", {name: "Advanced recipe document"});
  expect(nextAdvanced).toHaveAttribute("open");
  expect(within(nextAdvanced).getByRole("textbox", {name: "Recipe JSON"})).toHaveValue(JSON.stringify(fullLibraryDetail.visual_recipe, null, 2));
});

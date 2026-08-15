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

test("resets visual and raw preview state on canonical refresh and A to B to A navigation", () => {
  // Break caught: a same-revision content refresh or returning to recipe A can
  // resurrect a local draft that no longer matches canonical authority.
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

  const refreshedVisual = {
    ...fullLibraryDetail.visual_recipe!,
    workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/server-refresh"},
  };
  const refreshedDetail = {...fullLibraryDetail, visual_recipe: refreshedVisual};
  view.rerender(<LibraryRecipeAuthority {...props} detail={refreshedDetail}/>);
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/server-refresh");
  let nextAdvanced = screen.getByRole("group", {name: "Advanced recipe document"});
  expect(nextAdvanced).not.toHaveAttribute("open");
  fireEvent.click(within(nextAdvanced).getByText("Advanced recipe document"));
  expect(within(nextAdvanced).getByRole("textbox", {name: "Recipe JSON"})).toHaveValue(JSON.stringify(refreshedVisual, null, 2));

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
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/code");
  view.rerender(<LibraryRecipeAuthority {...props} detail={fullLibraryDetail}/>);
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("qwen/3");
  expect(screen.getByRole("region", {name: "Model and runtime"})).not.toHaveTextContent("qwen/local-preview");
  nextAdvanced = screen.getByRole("group", {name: "Advanced recipe document"});
  expect(nextAdvanced).not.toHaveAttribute("open");
  fireEvent.click(within(nextAdvanced).getByText("Advanced recipe document"));
  expect(within(nextAdvanced).getByRole("textbox", {name: "Recipe JSON"})).toHaveValue(JSON.stringify(fullLibraryDetail.visual_recipe, null, 2));
});

import {fireEvent, render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {LibraryApi} from "../api/types";
import {fullLibraryDetail, librarySnapshot} from "../test-fixtures/library";
import {LibraryRecipeAuthority} from "./library-recipe-detail";

function renderAuthority() {
  render(<LibraryRecipeAuthority api={{} as LibraryApi} detail={fullLibraryDetail} onRefresh={async () => undefined} policy={librarySnapshot.freshness_policy}/>);
}

test("previews exact catalog identities without prototype runtime adapters", async () => {
  renderAuthority();
  const user = userEvent.setup();
  const runtime = screen.getByRole("region", {name: "Model and runtime"});
  expect(runtime).toHaveTextContent(`qwen/qwen3@${"e".repeat(64)}`);
  expect(runtime).toHaveTextContent(`vonk-forge/vllm-openai@${"f".repeat(64)}`);
  expect(runtime).toHaveTextContent(`vonk-forge/python-312-cuda@${"1".repeat(64)}`);
  expect(runtime).not.toHaveTextContent(/adapter version|runtime adapter/i);

  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  await user.click(within(advanced).getByText("Advanced recipe document"));
  const editor = within(advanced).getByRole("textbox", {name: "Recipe JSON"});
  const changed = {...fullLibraryDetail.visual_recipe!, model: {...fullLibraryDetail.visual_recipe!.model, slug: "qwen3-preview"}};
  fireEvent.change(editor, {target: {value: JSON.stringify(changed)}});
  expect(runtime).toHaveTextContent("qwen/qwen3-preview@");
});

test("keeps the last valid local preview when a strict visual identity is invalid", async () => {
  renderAuthority();
  const user = userEvent.setup();
  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  await user.click(within(advanced).getByText("Advanced recipe document"));
  const editor = within(advanced).getByRole("textbox", {name: "Recipe JSON"});
  const changed = {...fullLibraryDetail.visual_recipe!, runtime: {...fullLibraryDetail.visual_recipe!.runtime, distribution: {...fullLibraryDetail.visual_recipe!.runtime.distribution, slug: "python-preview"}}};
  fireEvent.change(editor, {target: {value: JSON.stringify(changed)}});
  fireEvent.change(editor, {target: {value: JSON.stringify({...changed, model: {...changed.model, content_sha256: "not-a-digest"}})}});
  expect(within(advanced).getByRole("alert")).toHaveTextContent("$.model.content_sha256 must be 64 lowercase hexadecimal characters.");
  expect(screen.getByRole("region", {name: "Model and runtime"})).toHaveTextContent("vonk-forge/python-preview@");
});

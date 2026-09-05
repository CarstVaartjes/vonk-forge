import {fireEvent, render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi} from "../api/types";
import {fullLibraryDetail, librarySnapshot} from "../test-fixtures/library";
import {LibraryRecipeAuthority} from "./library-recipe-detail";

function renderAuthority() {
  render(<LibraryRecipeAuthority api={{} as ControlApi} detail={fullLibraryDetail} onRefresh={async () => undefined} policy={librarySnapshot.freshness_policy}/>);
}

test("previews friendly runtime names with exact identities available on demand", async () => {
  renderAuthority();
  const user = userEvent.setup();
  const runtime = screen.getByRole("region", {name: "Recipe identity"});
  expect(runtime).toHaveTextContent("Qwen 3");
  expect(runtime).toHaveTextContent("vLLM OpenAI");
  expect(runtime).toHaveTextContent("Python 312 CUDA");
  const technicalDetails = within(runtime).getAllByText("Technical details");
  await user.click(technicalDetails[0]);
  await user.click(technicalDetails[1]);
  await user.click(technicalDetails[2]);
  expect(runtime).toHaveTextContent(`qwen/qwen3@${"e".repeat(64)}`);
  expect(runtime).toHaveTextContent(`vonk-forge/vllm-openai@${"f".repeat(64)}`);
  expect(runtime).toHaveTextContent(`vonk-forge/python-312-cuda@${"1".repeat(64)}`);
  expect(runtime).not.toHaveTextContent(/adapter version|runtime adapter/i);

  const advanced = screen.getByRole("group", {name: "Advanced recipe document"});
  await user.click(within(advanced).getByText("Advanced recipe document"));
  const editor = within(advanced).getByRole("textbox", {name: "Recipe JSON"});
  const changed = {...fullLibraryDetail.visual_recipe!, model: {...fullLibraryDetail.visual_recipe!.model, slug: "qwen3-preview"}};
  fireEvent.change(editor, {target: {value: JSON.stringify(changed)}});
  expect(runtime).toHaveTextContent("Qwen 3 Preview");
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
  expect(screen.getByRole("region", {name: "Recipe identity"})).toHaveTextContent("Python Preview");
});

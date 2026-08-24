import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi} from "../api/types";
import {App} from "../app";

const GIB = 1024 ** 3;
const exactDigests = {
  model: "a1".repeat(32),
  harness: "b2".repeat(32),
  runtime: "c3".repeat(32),
  context: "d4".repeat(32),
};
const exactArtifactRevision = "e5".repeat(20);

function replaceFieldValue(field: HTMLElement, value: string) {
  fireEvent.change(field, {target: {value}});
}

afterEach(() => {
  history.replaceState(null, "", "/");
  sessionStorage.clear();
  vi.restoreAllMocks();
});

async function continueToReview(user: ReturnType<typeof userEvent.setup>) {
  const model = screen.queryByRole("textbox", {name: "Exact model digest"});
  if (model) {
    replaceFieldValue(model, exactDigests.model);
  }
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(await screen.findByRole("heading", {name: "Runtime"})).toBeVisible();
  for (const [name, value] of [["Exact harness digest", exactDigests.harness], ["Exact runtime digest", exactDigests.runtime], ["Exact build context digest", exactDigests.context]] as const) {
    const field = screen.getByRole("textbox", {name});
    replaceFieldValue(field, value);
  }
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(await screen.findByRole("heading", {name: "Artifacts"})).toBeVisible();
  const revision = screen.getByRole("textbox", {name: "Immutable revision"});
  replaceFieldValue(revision, exactArtifactRevision);
  for (const heading of ["Resources & topology", "Validation & provenance", "Review & create"]) {
    await user.click(screen.getByRole("button", {name: "Continue"}));
    expect(await screen.findByRole("heading", {name: heading})).toBeVisible();
  }
}

test("guides a preset through six steps and submits the complete canonical document", async () => {
  history.replaceState(null, "", "/library/create");
  const createCatalogRecipe = vi.fn(async (input: {slug: string; document: Record<string, unknown>}) => ({
    recipe_id: "custom-1", revision_number: 1, lifecycle: "draft", slug: input.slug, document: input.document,
  }));
  const user = userEvent.setup();
  render(<App api={{createCatalogRecipe} as unknown as ControlApi}/>);

  expect(await screen.findByRole("heading", {name: "Create custom recipe"})).toHaveFocus();
  expect(screen.getByRole("navigation", {name: "Recipe builder progress"})).toBeVisible();
  expect(screen.getByText("Step 1 of 6")).toBeVisible();

  await user.selectOptions(screen.getByRole("combobox", {name: "Recipe starting point"}), "vllm");
  await user.click(screen.getByRole("button", {name: "Apply starting point"}));
  expect(screen.getByRole("textbox", {name: "Display name"})).toHaveValue("vLLM chat service");

  const modelDigest = screen.getByRole("textbox", {name: "Exact model digest"});
  replaceFieldValue(modelDigest, exactDigests.model);
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(await screen.findByRole("heading", {name: "Runtime"})).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Previous"}));
  expect(await screen.findByRole("heading", {name: "Identity & model"})).toBeVisible();

  await continueToReview(user);
  expect(screen.getByText("vLLM chat service")).toBeVisible();
  expect(screen.getByText("local/custom-vllm-chat")).toBeVisible();
  expect(screen.getByText("80 GiB")).toBeVisible();
  expect(screen.getByText("Technical identities and digests")).toBeVisible();

  await user.click(screen.getByRole("button", {name: "Create recipe"}));
  expect(createCatalogRecipe).toHaveBeenCalledWith(expect.objectContaining({
    slug: "custom-vllm-chat",
    document: expect.objectContaining({
      schema_version: 1,
      build: expect.objectContaining({arguments: [], network: expect.any(Object), resources: expect.any(Object)}),
      artifacts: [expect.objectContaining({download_bytes: 80 * GIB, mount: {target: "/models", read_only: true}, roles: ["entrypoint"]})],
      interfaces: [expect.objectContaining({adapter: "openai", port: 8000})],
      runtime: expect.objectContaining({entrypoint: ["python", "-m", "vllm.entrypoints.openai.api_server"], arguments: [], environment: [], security: expect.any(Object), lifecycle: expect.any(Object)}),
      topology: expect.objectContaining({roles: expect.any(Array), parallelism: expect.any(Object)}),
      validation: expect.objectContaining({validators: [expect.objectContaining({interface: "openai"})]}),
    }),
  }));
  expect(await screen.findByText("Recipe saved")).toBeVisible();
  expect(screen.getByRole("button", {name: "View saved recipe"})).toBeVisible();
  expect(sessionStorage.getItem("vonk-forge:custom-recipe-draft:v1")).toBeNull();
});

test("requires starter component digests to be replaced before continuing", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);

  await user.click(await screen.findByRole("button", {name: "Continue"}));
  const summary = screen.getByRole("alert");
  expect(summary).toHaveTextContent("Model digest is still a starter placeholder. Replace it with the exact SHA-256.");
  expect(screen.getByRole("textbox", {name: "Exact model digest"})).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText("Step 1 of 6")).toBeVisible();
});

test("blocks placeholder artifact revisions and required empty collections", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);

  const model = await screen.findByRole("textbox", {name: "Exact model digest"});
  replaceFieldValue(model, exactDigests.model);
  await user.click(screen.getByRole("button", {name: "Continue"}));
  for (const [name, value] of [["Exact harness digest", exactDigests.harness], ["Exact runtime digest", exactDigests.runtime], ["Exact build context digest", exactDigests.context]] as const) {
    const field = screen.getByRole("textbox", {name});
    replaceFieldValue(field, value);
  }
  await user.click(screen.getByRole("button", {name: "Continue"}));
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(screen.getByRole("alert")).toHaveTextContent("revision is still a starter placeholder");

  await user.click(screen.getByRole("button", {name: "Remove artifact 1"}));
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(screen.getByRole("alert")).toHaveTextContent("Add at least one immutable artifact.");
});

test("requires an interface and a bound validator before review", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);
  await continueToReview(user);

  await user.click(screen.getByRole("button", {name: "Change resources and topology"}));
  await user.click(screen.getByRole("button", {name: "Remove interface 1"}));
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(screen.getByRole("alert")).toHaveTextContent("Add at least one service interface.");

  await user.click(screen.getByRole("button", {name: "Add interface"}));
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(await screen.findByRole("heading", {name: "Validation & provenance"})).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(screen.getByRole("alert")).toHaveTextContent("Add at least one validator and check.");
});

test("blocks forward navigation with a focused error summary and inline field error", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);

  const digest = await screen.findByRole("textbox", {name: "Exact model digest"});
  await user.clear(digest);
  await user.type(digest, "not-a-digest");
  await user.click(screen.getByRole("button", {name: "Continue"}));

  const summary = screen.getByRole("alert");
  expect(summary).toHaveFocus();
  expect(within(summary).getByRole("link", {name: "Model digest must be 64 lowercase hexadecimal characters."})).toHaveAttribute("href", "#model-digest");
  expect(digest).toHaveAttribute("aria-invalid", "true");
  await user.click(within(summary).getByRole("link", {name: "Model digest must be 64 lowercase hexadecimal characters."}));
  expect(digest).toHaveFocus();
  expect(screen.getByText("Step 1 of 6")).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Runtime"})).not.toBeInTheDocument();
});

test("edits byte values in human units while keeping exact bytes in advanced JSON", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);

  const model = await screen.findByRole("textbox", {name: "Exact model digest"});
  replaceFieldValue(model, exactDigests.model);
  await user.click(screen.getByRole("button", {name: "Continue"}));
  for (const [name, value] of [["Exact harness digest", exactDigests.harness], ["Exact runtime digest", exactDigests.runtime], ["Exact build context digest", exactDigests.context]] as const) {
    const field = screen.getByRole("textbox", {name});
    replaceFieldValue(field, value);
  }
  await user.click(screen.getByRole("button", {name: "Continue"}));
  const revision = screen.getByRole("textbox", {name: "Immutable revision"});
  replaceFieldValue(revision, exactArtifactRevision);
  await user.click(screen.getByRole("button", {name: "Continue"}));
  expect(await screen.findByRole("heading", {name: "Resources & topology"})).toBeVisible();

  const memory = screen.getByRole("spinbutton", {name: "Build memory amount"});
  expect(memory).toHaveValue(16);
  expect(screen.getByRole("combobox", {name: "Build memory unit"})).toHaveValue("3");
  await user.clear(memory);
  await user.type(memory, "24");
  expect(screen.getByText("24 GiB")).toBeVisible();

  await user.click(screen.getByText("Advanced JSON"));
  expect((screen.getByRole("textbox", {name: "Recipe document"}) as HTMLTextAreaElement).value).toContain(`"memory_bytes": ${24 * GIB}`);
});

test("keeps invalid advanced JSON for correction without corrupting the guided form", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);

  await user.click(await screen.findByText("Advanced JSON"));
  const json = screen.getByRole("textbox", {name: "Recipe document"});
  fireEvent.change(json, {target: {value: "{"}});
  expect(json).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText(/Invalid JSON at line 1/)).toBeVisible();
  expect(screen.getByRole("textbox", {name: "Display name"})).toHaveValue("Custom model service");
});

test("keeps deeply malformed advanced JSON out of guided state", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={{} as unknown as ControlApi}/>);

  await user.click(await screen.findByText("Advanced JSON"));
  const json = screen.getByRole("textbox", {name: "Recipe document"});
  const malformed = JSON.parse((json as HTMLTextAreaElement).value) as Record<string, unknown>;
  malformed.identity = {};
  fireEvent.change(json, {target: {value: JSON.stringify(malformed)}});

  expect(json).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText("$.identity.publisher must be a string.")).toBeVisible();
  expect(screen.getByRole("textbox", {name: "Publisher"})).toHaveValue("local");
  expect(screen.getByRole("textbox", {name: "Recipe slug"})).toHaveValue("custom-service");
});

test("restores an unsaved session draft and confirms before replacing it with a preset", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  const first = render(<App api={{} as unknown as ControlApi}/>);

  const title = await screen.findByRole("textbox", {name: "Display name"});
  fireEvent.change(title, {target: {value: "My carefully tuned recipe"}});
  await waitFor(() => expect(sessionStorage.getItem("vonk-forge:custom-recipe-draft:v1")).not.toBeNull());

  await user.selectOptions(screen.getByRole("combobox", {name: "Recipe starting point"}), "vllm");
  await user.click(screen.getByRole("button", {name: "Apply starting point"}));
  const replacement = screen.getByRole("alert");
  expect(replacement).toHaveTextContent("Replace the current draft?");
  expect(within(replacement).getByRole("button", {name: "Keep current draft"})).toHaveFocus();
  expect(title).toHaveValue("My carefully tuned recipe");
  await user.click(within(replacement).getByRole("button", {name: "Keep current draft"}));
  expect(title).toHaveValue("My carefully tuned recipe");

  first.unmount();
  history.replaceState(null, "", "/library/create");
  render(<App api={{} as unknown as ControlApi}/>);
  expect(await screen.findByText("Unsaved draft restored.")).toBeVisible();
  expect(screen.getByRole("textbox", {name: "Display name"})).toHaveValue("My carefully tuned recipe");
});

test("restores malformed advanced JSON with its validation state and last valid guided values", async () => {
  history.replaceState(null, "", "/library/create");
  const first = render(<App api={{} as unknown as ControlApi}/>);

  fireEvent.change(await screen.findByRole("textbox", {name: "Display name"}), {target: {value: "Last valid guided value"}});
  await waitFor(() => expect(sessionStorage.getItem("vonk-forge:custom-recipe-draft:v1")).not.toBeNull());
  const stored = JSON.parse(sessionStorage.getItem("vonk-forge:custom-recipe-draft:v1")!) as Record<string, unknown>;
  sessionStorage.setItem("vonk-forge:custom-recipe-draft:v1", JSON.stringify({...stored, documentText: "{"}));

  first.unmount();
  history.replaceState(null, "", "/library/create");
  render(<App api={{} as unknown as ControlApi}/>);
  await userEvent.setup().click(await screen.findByText("Advanced JSON"));

  expect(screen.getByRole("textbox", {name: "Recipe document"})).toHaveValue("{");
  expect(screen.getByRole("textbox", {name: "Recipe document"})).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByText(/Invalid JSON at line 1/)).toBeVisible();
  expect(screen.getByRole("textbox", {name: "Display name"})).toHaveValue("Last valid guided value");
});

test("locks navigation and form controls during save and surfaces an API rejection", async () => {
  history.replaceState(null, "", "/library/create");
  let rejectSave: (reason: Error) => void = () => undefined;
  const createCatalogRecipe = vi.fn(() => new Promise<never>((_resolve, reject) => { rejectSave = reject; }));
  const user = userEvent.setup();
  render(<App api={{createCatalogRecipe} as unknown as ControlApi}/>);
  await continueToReview(user);
  await user.click(screen.getByText("Advanced JSON"));

  await user.click(screen.getByRole("button", {name: "Create recipe"}));
  expect(screen.getByRole("button", {name: "Creating recipe…"})).toBeDisabled();
  expect(screen.getByRole("textbox", {name: "Recipe document"})).toBeDisabled();
  expect(screen.getByRole("button", {name: "Back to Library"})).toBeDisabled();
  expect(createCatalogRecipe).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", {name: "Creating recipe…"}));
  expect(createCatalogRecipe).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("link", {name: "Fleet"}));
  expect(location.pathname).toBe("/library/create");
  rejectSave(new Error("Catalog write was rejected"));

  expect(await screen.findByRole("alert")).toHaveTextContent("Catalog write was rejected");
  await waitFor(() => expect(screen.getByRole("button", {name: "Create recipe"})).toBeEnabled());
  expect(createCatalogRecipe).toHaveBeenCalledTimes(1);
});

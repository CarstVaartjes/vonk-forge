import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {RecipeEditorPage} from "./recipe-editor";

test("uploads source first and authors a source-first typed recipe", async () => {
  const created: unknown[] = [];
  const uploaded: string[] = [];
  const api = {
    uploadSourceBundle: async (sha256: string) => {
      uploaded.push(sha256);
      return {sha256, archive_bytes: 1, total_bytes: 1, file_count: 1, files: ["Dockerfile"]};
    },
    createCatalogRecipe: async (input: unknown) => {
      created.push(input);
      return {recipe_id: "10000000-0000-4000-8000-000000000001", revision_number: 1};
    },
  };
  render(<RecipeEditorPage api={api as any}/>);
  const user = userEvent.setup();

  expect(screen.getByRole("heading", {name: "Create local recipe"})).toBeVisible();
  expect(screen.getByRole("region", {name: "Recipe authoring steps"})).toBeVisible();
  expect((screen.getByLabelText("Dockerfile") as HTMLTextAreaElement).value).toContain("USER 65532:65532");

  await user.type(screen.getByLabelText("Recipe slug"), "my-model");
  await user.type(screen.getByLabelText("Title"), "My model");
  await user.type(screen.getByLabelText("Description"), "A locally authored model recipe.");
  await user.type(screen.getByLabelText("Model-version slug"), "my-model-fp16");
  await user.type(screen.getByLabelText("Artifact repository"), "Example/MyModel");
  await user.type(screen.getByLabelText("Artifact revision"), "0123456789abcdef0123456789abcdef01234567");
  await user.clear(screen.getByLabelText("Artifact bytes"));
  await user.type(screen.getByLabelText("Artifact bytes"), "1000000");
  await user.click(screen.getByRole("button", {name: "Verify source & save draft"}));
  expect(await screen.findByRole("status")).toHaveTextContent("Source verified and draft saved as revision 1");

  expect(uploaded).toHaveLength(1);
  expect(created).toHaveLength(1);
  const input = created[0] as {slug: string; document: Record<string, any>};
  expect(input.slug).toBe("my-model");
  expect(input.document.runtime.security.privileged).toBe(false);
  expect(input.document.runtime.security.host_network).toBe(false);
  expect(input.document.build.platform).toBe("linux/arm64");
  expect(input.document.model).toEqual(expect.objectContaining({kind: "model-version", slug: "my-model-fp16"}));
  expect(input.document.execution.harness).toEqual(expect.objectContaining({kind: "execution-harness"}));
  expect(input.document.runtime.distribution).toEqual(expect.objectContaining({kind: "runtime-distribution"}));
  expect(input.document.runtime).not.toHaveProperty("adapter");
  expect(input.document.build.context.sha256).toBe(uploaded[0]);
  expect(input.document.topology.node_count).toBe(1);
});

test("authors a valid multi-node entrypoint and worker topology", async () => {
  const created: Array<{document: Record<string, any>}> = [];
  const api = {
    uploadSourceBundle: async (sha256: string) => ({sha256, archive_bytes: 1, total_bytes: 1, file_count: 1, files: ["Dockerfile"]}),
    createCatalogRecipe: async (input: {document: Record<string, any>}) => {
      created.push(input);
      return {recipe_id: "10000000-0000-4000-8000-000000000002", revision_number: 1};
    },
  };
  render(<RecipeEditorPage api={api as any}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText("Recipe slug"), "two-node-model");
  await user.type(screen.getByLabelText("Title"), "Two node model");
  await user.type(screen.getByLabelText("Description"), "A valid two node recipe.");
  await user.type(screen.getByLabelText("Model-version slug"), "two-node-model-fp16");
  await user.type(screen.getByLabelText("Artifact repository"), "Example/TwoNode");
  await user.type(screen.getByLabelText("Artifact revision"), "0123456789abcdef0123456789abcdef01234567");
  await user.clear(screen.getByLabelText("Topology node count"));
  await user.type(screen.getByLabelText("Topology node count"), "2");
  await user.click(screen.getByRole("button", {name: "Verify source & save draft"}));

  expect(await screen.findByRole("status")).toHaveTextContent("Source verified and draft saved as revision 1");
  const topology = created[0].document.topology;
  expect(topology.roles.map((role: {name: string; count: number; endpoint_owner: boolean}) => [role.name, role.count, role.endpoint_owner])).toEqual([
    ["entrypoint", 1, true],
    ["worker", 1, false],
  ]);
  expect(topology.start_order).toEqual(["entrypoint", "worker"]);
  expect(topology.stop_order).toEqual(["worker", "entrypoint"]);
  expect(created[0].document.artifacts[0].roles).toEqual(["entrypoint", "worker"]);
});

test("attaches local test evidence and exports for an exact publisher namespace", async () => {
  const recipe = {
    recipe_id: "10000000-0000-4000-8000-000000000001", revision_number: 2,
    slug: "qwen", title: "Qwen", description: "Test", origin: "local", lifecycle: "resolved",
    content_sha256: "a".repeat(64), schema_version: 1, created_by: "admin", created_at: "2026-08-07T10:00:00Z",
    document: {schema_version: 1, identity: {publisher: "local", slug: "qwen"}, model: {kind: "model-version", publisher: "local", slug: "qwen-fp16", content_sha256: "b".repeat(64)}, execution: {harness: {kind: "execution-harness", publisher: "local", slug: "vllm-openai", content_sha256: "c".repeat(64)}, patch_bundle: null}, runtime: {distribution: {kind: "runtime-distribution", publisher: "local", slug: "python-312-cuda", content_sha256: "d".repeat(64)}, entrypoint: ["vllm"]}, topology: {name: "solo", mode: "single", node_count: 1}, interfaces: [{adapter: "openai", port: 8000, model_aliases: ["qwen"], health_path: "/v1/models"}]},
  } as any;
  const reports: unknown[] = [];
  const exports: string[] = [];
  const api = {
    createCatalogRecipe: async () => recipe,
    catalogRecipe: async () => recipe,
    attachPublicationReport: async (_id: string, report: unknown) => { reports.push(report); },
    publicationExport: async (_id: string, publisher: string) => { exports.push(publisher); return {recipe: recipe.document, test_report: {}}; },
  };
  const createObjectURL = vi.fn(() => "blob:export");
  Object.defineProperty(URL, "createObjectURL", {value: createObjectURL, configurable: true});
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  render(<RecipeEditorPage api={api} recipeId={recipe.recipe_id}/>);
  const user = userEvent.setup();
  const report = {schema_version: 1, recipe_sha256: "a".repeat(64)};

  await screen.findByText(/Canonical content/);
  await user.upload(screen.getByLabelText("Local test report JSON"), new File([JSON.stringify(report)], "report.json", {type: "application/json"}));
  expect(reports).toEqual([report]);
  await user.clear(screen.getByLabelText("Target publisher namespace"));
  await user.type(screen.getByLabelText("Target publisher namespace"), "ada-lab");
  await user.click(screen.getByRole("button", {name: "Download publication JSON"}));

  expect(exports).toEqual(["ada-lab"]);
  expect(createObjectURL).toHaveBeenCalled();
  expect(click).toHaveBeenCalled();
  click.mockRestore();
});

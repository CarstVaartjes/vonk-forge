import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {CatalogPage} from "./catalog";

const recipes = [
  {recipe_id: "10000000-0000-4000-8000-000000000001", slug: "local-qwen", title: "Local Qwen", origin: "local" as const, revision_number: 2, lifecycle: "resolved" as const, content_sha256: "a".repeat(64), source_bundle_sha256: "b".repeat(64), execution_harness: "vllm-openai", runtime_distribution: "python-312-cuda", topology_name: "solo", topology_mode: "single", node_count: 1, artifact_count: 1, expected_download_bytes: 61_000_000_000, maximum_installed_bytes_per_node: 66_000_000_000, maximum_runtime_memory_bytes_per_node: 80_000_000_000},
  {recipe_id: "10000000-0000-4000-8000-000000000002", slug: "deepseek", title: "DeepSeek", origin: "workload_run" as const, revision_number: 1, lifecycle: "draft" as const, content_sha256: null, source_bundle_sha256: "c".repeat(64), execution_harness: "sglang-openai", runtime_distribution: "python-312-cuda", topology_name: "tensor-pair", topology_mode: "tensor_parallel", node_count: 2, artifact_count: 2, expected_download_bytes: 120_000_000_000, maximum_installed_bytes_per_node: 130_000_000_000, maximum_runtime_memory_bytes_per_node: 100_000_000_000},
  {recipe_id: "10000000-0000-4000-8000-000000000003", slug: "global-model", title: "Global model", origin: "global" as const, revision_number: 4, lifecycle: "resolved" as const, content_sha256: "d".repeat(64), source_bundle_sha256: "e".repeat(64), execution_harness: "llama-cpp-server", runtime_distribution: "cuda-runtime", topology_name: "solo", topology_mode: "single", node_count: 1, artifact_count: 1, expected_download_bytes: 10_000_000_000, maximum_installed_bytes_per_node: 12_000_000_000, maximum_runtime_memory_bytes_per_node: 18_000_000_000},
];

test("separates local, WorkloadRun, and global recipe origins", async () => {
  render(<CatalogPage api={{catalogRecipes: async () => ({recipes, next_cursor: null})}}/>);

  expect(await screen.findByRole("heading", {name: "Recipe catalog"})).toBeVisible();
  expect(screen.getByText("Local")).toBeVisible();
  expect(screen.getByText("Imported from WorkloadRun")).toBeVisible();
  expect(screen.getByText("Downloaded from vonkforge.ai")).toBeVisible();
  expect(screen.getByText("up to 66.0 GB disk / node")).toBeVisible();
  expect(screen.getByText("up to 80.0 GB RAM / node")).toBeVisible();
  expect(screen.getByText("tensor-pair · 2 nodes")).toBeVisible();
  expect(screen.getByRole("link", {name: "Create local recipe"})).toHaveAttribute("href", "/catalog/new");
});

test("reviews an exact public revision before importing it locally", async () => {
  const imported: string[] = [];
  const uri = `vonk://catalog/vonk/qwen3-vllm@sha256:${"a".repeat(64)}`;
  const api = {
    catalogRecipes: async () => ({recipes: [], next_cursor: null}),
    previewGlobalRecipe: async (value: string) => ({
      publisher: "vonk", slug: "qwen3-vllm", revision_number: 2,
      recipe_id: "00000000-0000-4000-8000-000000000002",
      revision_id: "10000000-0000-4000-8000-000000000002",
      content_sha256: "a".repeat(64), published_at: "2026-08-07T10:00:00Z",
      document: {metadata: {title: "Qwen3"}, build: {context: {sha256: "f".repeat(64)}, dockerfile: "Dockerfile"}, topology: {name: "solo", node_count: 1, roles: [{resources: {disk: {image_bytes: 5_000_000_000, artifact_bytes: 53_000_000_000, staging_bytes: 8_000_000_000, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 0}, memory: {startup_peak_bytes: 72_000_000_000}}}]}},
    }),
    importGlobalRecipe: async (value: string, digest: string) => {
      imported.push(`${value}:${digest}`);
      return {...recipes[2], id: "20000000-0000-4000-8000-000000000001", description: "Imported", schema_version: 1 as const, document: {}, created_by: "admin", created_at: "2026-08-07T10:00:00Z"};
    },
  };
  render(<CatalogPage api={api}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText("Immutable vonkforge.ai URI"), uri);
  await user.click(screen.getByRole("button", {name: "Review global recipe"}));

  expect(await screen.findByRole("heading", {name: "Review Qwen3"})).toBeVisible();
  expect(screen.getByText("66.0 GB disk / node")).toBeVisible();
  expect(screen.getByText("72.0 GB RAM / node")).toBeVisible();
  expect(screen.getByText(`sha256:${"f".repeat(64)}`)).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Import exact revision"}));
  expect(imported).toEqual([`${uri}:${"a".repeat(64)}`]);
  expect(await screen.findByRole("status")).toHaveTextContent("available offline");
});

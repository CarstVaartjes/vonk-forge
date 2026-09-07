import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import {DeploymentProvenanceView} from "./deployment-provenance-view";

const evidence = {source: "Authenticated agent contact", observed_at: "2026-09-08T10:00:00Z", age_seconds: 7200, freshness: "stale"};
const snapshot = {
  schema_version: 2, generated_at: "2026-09-08T12:00:00Z",
  platform: [{boundary: "repository", state: "repository_not_published", source_commit: "a".repeat(40), evidence}, {boundary: "publication", state: "publication_not_deployed", source_commit: "b".repeat(40), evidence}, {boundary: "controller_deployment", state: "observed", source_commit: "c".repeat(40), evidence}],
  recipe_library: {repository: null, source_commit: null, state: "unknown", evidence},
  agents: [{node_id: "spark-a", display_name: "Spark A", connectivity: "offline", semantic_version: "1.2.3", binary_sha256: "d".repeat(64), evidence}],
  workloads: [{installation_id: "installation-a", installation_state: "installed", recipe_publisher: "vonk", recipe_slug: "qwen", recipe_revision_id: "recipe-a", recipe_revision_number: 1, recipe_content_sha256: "e".repeat(64), image_digest: "sha256:" + "f".repeat(64), mapping_generation: 1, run_generation: 2, run_id: "run-a", run_state: "running", rank_agreement: "mismatch", models: [], physical_acceptance: {state: "not_qualified", evidence}, ranks: [{node_id: "spark-a", rank: 0, role: "leader", installation_state: "installed", runtime_state: "running", identity_agreement: "mismatch", runtime_evidence: evidence}]}],
};

describe("Deployment provenance", () => {
  it("keeps unpublished, undeployed, offline, mismatch and qualification states distinct", async () => {
    const api = {deploymentProvenance: vi.fn().mockResolvedValue(snapshot)};
    render(<DeploymentProvenanceView api={api}/>);
    expect(await screen.findByText("Repository differs from publication")).toBeVisible();
    expect(screen.getByText("Publication differs from deployment")).toBeVisible();
    expect(screen.getByText("Offline · last observed")).toBeVisible();
    expect(screen.getAllByText("Identity mismatch").length).toBe(2);
    expect(screen.getByText("Not physically qualified")).toBeVisible();
    expect(screen.getAllByText(/Stale · 2h ago/).length).toBeGreaterThan(1);
    expect(screen.getByText("Complete evidence JSON")).toBeVisible();
    fireEvent.click(screen.getByRole("button", {name: "Refresh evidence"}));
    await waitFor(() => expect(api.deploymentProvenance).toHaveBeenCalledTimes(2));
  });
  it("recovers from a failed projection without inventing empty evidence", async () => {
    const api = {deploymentProvenance: vi.fn().mockRejectedValueOnce(new Error("failure")).mockResolvedValue({...snapshot, agents: [], workloads: []})};
    render(<DeploymentProvenanceView api={api}/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Deployment evidence could not be loaded");
    fireEvent.click(screen.getByRole("button", {name: "Refresh evidence"}));
    expect(await screen.findByText("No Spark deployment evidence recorded.")).toBeVisible();
    expect(screen.getByText("No active installations recorded.")).toBeVisible();
  });
});

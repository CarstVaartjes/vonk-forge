import {fireEvent, render, screen} from "@testing-library/react";
import {expect, test, vi} from "vitest";
import {LibraryAvailabilityOperation, selectAvailabilityOperation, type AvailabilityOperationPresentation} from "./library-availability-operation";

const running = (phase: string, completed_bytes: number, total_bytes?: number) => ({phase, completed_bytes, ...(total_bytes === undefined ? {total_bytes_known: false} : {total_bytes, total_bytes_known: true}), bytes_per_second: 64, eta_seconds: total_bytes === undefined ? undefined : 20});

function fixture(overrides: Partial<AvailabilityOperationPresentation> = {}): AvailabilityOperationPresentation {
  return {
    id: "availability-op-1",
    requestId: "request-1",
    recipeRevisionId: "recipe-revision-1",
    state: "running",
    attempt: 1,
    progress: running("prepare", 6, 20),
    members: [
      {key: "model-cache", label: "Model files", state: "running", progress: running("download", 10, 100)},
      {key: "runtime-image", label: "Runtime image", state: "running", progress: running("build", 8)},
    ],
    runtimeMode: "build",
    ...overrides,
  };
}

test("shows one action with simultaneous Model and image/build member progress", () => {
  const makeAvailable = vi.fn();
  render(<><LibraryAvailabilityOperation operation={fixture()} onMakeAvailable={makeAvailable}/><button type="button">Unrelated Model action</button></>);

  expect(screen.getByRole("button", {name: "Preparing…"})).toBeDisabled();
  expect(screen.getByText("Model files")).toBeInTheDocument();
  expect(screen.getByText("Runtime image")).toBeInTheDocument();
  expect(screen.getAllByRole("progressbar")).toHaveLength(3);
  expect(screen.getByRole("button", {name: "Unrelated Model action"})).toBeEnabled();
  expect(makeAvailable).not.toHaveBeenCalled();
});

test("presents access recovery, canonical Model access link, and provider countdown", () => {
  const checkAccess = vi.fn();
  render(<LibraryAvailabilityOperation modelAccessUrl="https://huggingface.co/acme/model" onCheckAccessAndResume={checkAccess} operation={fixture({
    failure: {code: "rate_limited", detail: "Provider is limiting requests.", recovery_actions: ["resume", "retry"], retryable: true, retry_after_seconds: 3, retry_time: "2026-09-06T14:00:03Z"},
    members: [{key: "model-cache", state: "failed", progress: running("download", 18, 100), failure: {code: "access_required", detail: "Hugging Face access is required.", recovery_actions: ["open_model_access", "configure_hf_token", "check_access_and_resume"], retryable: true}}, {key: "runtime-image", state: "queued", progress: running("queued", 0)}],
  })} onRetry={() => undefined}/>);

  expect(screen.getByRole("link", {name: "Open Model access page"})).toHaveAttribute("href", "https://huggingface.co/acme/model");
  expect(screen.getByText(/existing protected HF token secret file/)).toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Retry in 3s"})).toBeDisabled();
  fireEvent.click(screen.getAllByRole("button", {name: "Check access and resume"})[0]!);
  expect(checkAccess).toHaveBeenCalledOnce();
  expect(screen.getByText("Provider is limiting requests.")).toBeInTheDocument();
});

test("keeps NAS, integrity, and build failure details bounded and actionable", () => {
  const retry = vi.fn();
  render(<LibraryAvailabilityOperation operation={fixture({
    state: "failed",
    failure: {code: "capacity_insufficient", detail: "Not enough NAS space.", recovery_actions: ["free_space"], retryable: true, required_bytes: 200, free_bytes: 100, shortfall_bytes: 100},
    members: [
      {key: "model-cache", state: "failed", progress: running("verify", 80, 100), failure: {code: "integrity_mismatch", detail: "The selected bytes failed verification.", recovery_actions: ["download_again"], retryable: false, preserved: "Previous valid object retained."}},
      {key: "runtime-image", state: "failed", progress: running("build", 0), failure: {code: "build_failed", detail: "Image build failed at step 8.", recovery_actions: ["retry", "force_rebuild"], retryable: true, log_excerpt: "Step 8: compiling attention kernels\nAuthorization: Bearer secret"}},
    ],
  })} onRetry={retry}/>);

  expect(screen.getByText("200 bytes")).toBeInTheDocument();
  expect(screen.getByText(/Previous valid object retained/)).toBeInTheDocument();
  expect(screen.getByText(/Image build failed at step 8/)).toBeInTheDocument();
  expect(screen.getByText(/Step 8: compiling attention kernels/)).toBeInTheDocument();
  expect(screen.queryByText("Bearer secret")).not.toBeInTheDocument();
  expect(screen.getAllByRole("button", {name: /Retry/}).length).toBeGreaterThan(0);
});

test("force action names exact image behavior and never implies Model redownload", () => {
  const force = vi.fn();
  render(<LibraryAvailabilityOperation operation={fixture({state: "succeeded", runtimeMode: "image", result: {artifact_set_sha256: "a".repeat(64), image_digest: "sha256:image"}})} onForce={force}/>);
  fireEvent.click(screen.getByText("More actions"));
  expect(screen.getByRole("button", {name: "Download image again"})).toBeInTheDocument();
  expect(screen.getByText(/only the runtime image is refreshed/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name: "Download image again"}));
  expect(force).toHaveBeenCalledOnce();
  expect(screen.getByText("artifact_set_sha256: "+"a".repeat(64))).toBeInTheDocument();
});

test("resume selection is exact revision scoped after reload", () => {
  const selected = selectAvailabilityOperation([
    fixture({id: "old", recipeRevisionId: "recipe-revision-1", state: "succeeded", updatedAt: "2026-09-06T12:00:00Z"}),
    fixture({id: "active", recipeRevisionId: "recipe-revision-1", state: "running", updatedAt: "2026-09-06T11:00:00Z"}),
    fixture({id: "other", recipeRevisionId: "recipe-revision-2", state: "running", updatedAt: "2026-09-06T14:00:00Z"}),
  ], "recipe-revision-1");
  expect(selected?.id).toBe("active");
  expect(selectAvailabilityOperation([fixture({recipeRevisionId: "recipe-revision-2"})], "recipe-revision-1")).toBeUndefined();
});

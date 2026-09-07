import {fireEvent, render, screen} from "@testing-library/react";
import {expect, test, vi} from "vitest";
import {availabilityFailure, LibraryAvailabilityFeedback} from "./library-availability-feedback";

test("normalizes the shared failure fields and redacts secret-bearing log text", () => {
  const failure = availabilityFailure({
    id: "op-1",
    failure: {
      code: "model_cache.auth_required",
      detail: "Hugging Face access is required",
      recovery_actions: ["open_model_access", "check_access_and_resume"],
      retry_after_seconds: 4,
      log_excerpt: "Authorization: Bearer super-secret https://cdn.example.test/file?token=secret",
    },
  });
  expect(failure).toMatchObject({
    code: "model_cache.auth_required",
    detail: "Hugging Face access is required",
    recovery: ["Open the selected Model access page", "Check access and resume"],
    retryAfterSeconds: 4,
    operationId: "op-1",
  });
  expect(failure.logExcerpt).toContain("<redacted>");
  expect(failure.logExcerpt).toContain("<signed-url-redacted>");
  expect(failure.logExcerpt).not.toContain("super-secret");
});

test("renders preserved progress, recovery, and a retryable terminal action", () => {
  const retry = vi.fn();
  render(<LibraryAvailabilityFeedback failure={availabilityFailure({
    id: "op-2",
    failure: {
      code: "model_cache.digest_mismatch",
      detail: "Downloaded bytes failed verification.",
      recovery_actions: ["download_again"],
      retryable: true,
    },
    progress: {completed_bytes: 12},
  })} onRetry={retry} retryLabel="Download again"/>);
  expect(screen.getByRole("alert")).toHaveTextContent("Downloaded bytes failed verification.");
  expect(screen.getByText(/12 bytes of progress retained/)).toBeInTheDocument();
  expect(screen.getByRole("list", {name: "Recovery steps"})).toHaveTextContent("Download the exact selected bytes again");
  fireEvent.click(screen.getByRole("button", {name: "Download again"}));
  expect(retry).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText("Technical details"));
  expect(screen.getByText("model_cache.digest_mismatch")).toBeInTheDocument();
  expect(screen.getByText("op-2")).toBeInTheDocument();
});

test("keeps model access and token-file recovery actionable without exposing secrets", () => {
  render(<LibraryAvailabilityFeedback modelAccessUrl="https://huggingface.co/acme/model" failure={availabilityFailure({
    code: "access_required",
    detail: "Hugging Face access is required.",
    recovery_actions: ["open_model_access", "configure_hf_token", "check_access_and_resume"],
  })}/>);
  expect(screen.getByRole("link", {name: "Open Model access page"})).toHaveAttribute("href", "https://huggingface.co/acme/model");
  expect(screen.getByText(/existing protected HF token secret file/)).toBeInTheDocument();
  expect(screen.queryByText(/token=|Bearer /i)).not.toBeInTheDocument();
});

test("shows known NAS capacity shortfall without turning unknown values into zero", () => {
  const failure = availabilityFailure({
    code: "model_cache.capacity_insufficient",
    detail: "Not enough NAS space.",
    recovery_actions: ["free_space"],
    required_bytes: 200,
    free_bytes: 100,
    shortfall_bytes: 100,
  });
  expect(failure).toMatchObject({requiredBytes: 200, freeBytes: 100, shortfallBytes: 100});
  render(<LibraryAvailabilityFeedback failure={failure}/>);
  expect(screen.getByText("Required")).toBeInTheDocument();
  expect(screen.getByText("200 bytes")).toBeInTheDocument();
  expect(screen.getByText("Shortfall")).toBeInTheDocument();
});

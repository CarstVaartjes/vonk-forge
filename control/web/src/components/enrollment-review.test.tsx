import {fireEvent, render, screen} from "@testing-library/react";
import type {EnrollmentSummary} from "../api/types";
import {EnrollmentReview} from "./enrollment-review";

const enrollment: EnrollmentSummary = {
  id: "enrollment-1", node_id: "spk_0123456789abcdef0123456789abcdef", state: "pending",
  agent_digest: "agent", boot_id: "boot", created_at: "2026-08-18T10:00:00Z",
  csr_public_key_fingerprint: "csr", hardware_fingerprint: "hardware", host_key_fingerprint: "host",
};

test("shows an actionable inline error when approval fails", async () => {
  const onApprove = vi.fn().mockRejectedValue(new Error("review service unavailable"));
  render(<EnrollmentReview enrollment={enrollment} onApprove={onApprove} onReject={vi.fn()} />);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", {name: "Approve enrollment"}));
  expect(await screen.findByText("review service unavailable", {selector: "[role='alert']"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Approve enrollment"})).toBeEnabled();
});

import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, vi} from "vitest";
import {AdminMenu} from "./admin-menu";

afterEach(() => {
  history.replaceState(null, "", "/");
});

test("opens audit log from the operator menu and closes it without leaving Fleet", async () => {
  // Break caught: the authenticated operator menu stops exposing audit access
  // inline, or opening and closing the audit panel mutates Fleet navigation.
  history.replaceState(null, "", "/fleet");
  const user = userEvent.setup();
  const loadAudit = vi.fn().mockResolvedValue({
    events: [{
      request_id: "audit-1",
      actor: "admin",
      action: "package.rollout.approved",
      base_commit: "a".repeat(40),
      targets: ["node-1"],
    }],
  });

  render(<AdminMenu
    environment="Development"
    loadAudit={loadAudit}
    loggingOut={false}
    logoutError=""
    onLogout={() => undefined}
    role="Administrator"
    subject="admin"
  />);

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("button", {name: "Audit log"}));

  expect(await screen.findByText("package.rollout.approved")).toBeVisible();
  expect(screen.getByText("Actor admin")).toBeVisible();

  await user.click(screen.getByRole("button", {name: "Close audit log"}));

  await waitFor(() => {
    expect(screen.queryByText("package.rollout.approved")).not.toBeInTheDocument();
  });
  expect(location.pathname).toBe("/fleet");
});

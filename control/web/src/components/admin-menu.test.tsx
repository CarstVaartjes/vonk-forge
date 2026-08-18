import {render, screen, waitFor, within} from "@testing-library/react";
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
      action: "library.recipe.run.applied",
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

  const dialog = await screen.findByRole("dialog", {name: "Audit log"});
  expect(within(dialog).getByText("library.recipe.run.applied")).toBeVisible();
  expect(within(dialog).getByText("Actor admin")).toBeVisible();

  await user.click(within(dialog).getByRole("button", {name: "Close audit log"}));

  await waitFor(() => {
    expect(screen.queryByText("library.recipe.run.applied")).not.toBeInTheDocument();
  });
  expect(location.pathname).toBe("/fleet");
});

test("renders only the first eight audit events inside the compact audit drawer", async () => {
  // Break caught: the compact audit view stops bounding results, causing the
  // operator drawer to grow without limit inside the shell.
  const user = userEvent.setup();
  const loadAudit = vi.fn().mockResolvedValue({
    events: Array.from({length: 9}, (_, index) => ({
      request_id: `audit-${index + 1}`,
      actor: `admin-${index + 1}`,
      action: `action-${index + 1}`,
      base_commit: "a".repeat(40),
      targets: [`node-${index + 1}`],
    })),
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

  const dialog = await screen.findByRole("dialog", {name: "Audit log"});
  expect(within(dialog).getAllByRole("listitem")).toHaveLength(8);
  expect(within(dialog).getByText("action-8")).toBeVisible();
  expect(within(dialog).queryByText("action-9")).not.toBeInTheDocument();
});

test("shows a loading state inside the audit drawer while audit history is pending", async () => {
  // Break caught: opening audit history no longer exposes a pending state, so
  // the operator drawer appears empty during an in-flight authority request.
  const user = userEvent.setup();
  let resolveAudit!: (value: {events: never[]}) => void;
  const loadAudit = vi.fn().mockImplementation(() => new Promise(resolve => {
    resolveAudit = resolve;
  }));

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

  const dialog = await screen.findByRole("dialog", {name: "Audit log"});
  expect(within(dialog).getByRole("status")).toHaveTextContent("Loading audit log…");

  resolveAudit({events: []});
  await waitFor(() => {
    expect(within(dialog).queryByRole("status")).not.toBeInTheDocument();
  });
});

test("shows an audit error inside the drawer when audit history fails to load", async () => {
  // Break caught: a failed audit request no longer surfaces an error in the
  // operator drawer, leaving the operator without recovery context.
  const user = userEvent.setup();
  const loadAudit = vi.fn().mockRejectedValue(new Error("audit authority unavailable"));

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

  const dialog = await screen.findByRole("dialog", {name: "Audit log"});
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("audit authority unavailable");
});

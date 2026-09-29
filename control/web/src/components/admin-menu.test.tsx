import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {AdminMenu} from "./admin-menu";
import {ToastProvider} from "./toast";

function renderMenu(overrides: Partial<React.ComponentProps<typeof AdminMenu>> = {}) {
  const props: React.ComponentProps<typeof AdminMenu> = {
    onDownloadCliToken: vi.fn(async () => ({expiresAt: "2026-09-12T09:30:00Z"})),
    loggingOut: false,
    logoutError: "",
    onLogout: vi.fn(),
    onNavigate: vi.fn(event => event.preventDefault()),
    role: "Administrator",
    subject: "admin",
    ...overrides,
  };
  render(<ToastProvider><AdminMenu {...props}/></ToastProvider>);
  return props;
}

test("opens an operator disclosure and moves focus to its first action", async () => {
  const user = userEvent.setup();
  renderMenu();

  const trigger = screen.getByRole("button", {name: /admin/i});
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(trigger).not.toHaveAttribute("aria-haspopup");
  await user.click(trigger);

  const actions = screen.getByRole("group", {name: "Operator actions"});
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  expect(within(actions).getByRole("button", {name: "Download CLI token"})).toHaveFocus();
  expect(actions).toHaveClass("admin-menu-panel");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("supports disclosure keyboard navigation and restores trigger focus on Escape", async () => {
  const user = userEvent.setup();
  renderMenu();
  const trigger = screen.getByRole("button", {name: /admin/i});
  await user.click(trigger);

  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("link", {name: "API keys"})).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("link", {name: /Documentation/})).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("button", {name: "Logout"})).toHaveFocus();
  await user.keyboard("{ArrowUp}");
  expect(screen.getByRole("link", {name: /Documentation/})).toHaveFocus();
  await user.keyboard("{Escape}");

  expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("closes the menu before navigating to API keys", async () => {
  const user = userEvent.setup();
  const onNavigate = vi.fn(event => event.preventDefault());
  renderMenu({onNavigate});

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("link", {name: "API keys"}));

  expect(onNavigate).toHaveBeenCalledOnce();
  expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("closes on outside interaction", async () => {
  const user = userEvent.setup();
  const onNavigate = vi.fn(event => event.preventDefault());
  render(<div>
    <button type="button">Outside</button>
    <AdminMenu loggingOut={false} logoutError="" onDownloadCliToken={async () => ({expiresAt: ""})} onLogout={() => undefined} onNavigate={onNavigate} role="Administrator" subject="admin"/>
  </div>);

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("button", {name: "Outside"}));
  await waitFor(() => expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument());
});

test("closes and disables all operator actions while global navigation is locked", async () => {
  const user = userEvent.setup();
  const onLogout = vi.fn();
  const onNavigate = vi.fn(event => event.preventDefault());
  const props = {loggingOut: false, logoutError: "", onDownloadCliToken: vi.fn(async () => ({expiresAt: ""})), onLogout, onNavigate, role: "Administrator", subject: "admin"};
  const view = render(<AdminMenu {...props}/>);

  const trigger = screen.getByRole("button", {name: /admin/i});
  await user.click(trigger);
  expect(screen.getByRole("button", {name: "Logout"})).toBeEnabled();

  view.rerender(<AdminMenu {...props} navigationLocked/>);
  expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument();
  expect(trigger).toBeDisabled();
  expect(trigger).toHaveAttribute("title", "Operator actions are unavailable while a change is applying");
  await user.click(trigger);
  expect(onLogout).not.toHaveBeenCalled();
  expect(onNavigate).not.toHaveBeenCalled();
});

test("downloads a CLI token from the account menu and reports the result", async () => {
  const user = userEvent.setup();
  const onDownloadCliToken = vi.fn(async () => ({expiresAt: "2026-09-12T09:30:00Z"}));
  renderMenu({onDownloadCliToken});

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("button", {name: "Download CLI token"}));

  expect(onDownloadCliToken).toHaveBeenCalledOnce();
  expect(await within(screen.getByRole("region", {name: "Notifications"})).findByText(/CLI token downloaded/)).toBeVisible();
});

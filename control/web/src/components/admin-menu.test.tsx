import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {AdminMenu} from "./admin-menu";

function renderMenu(overrides: Partial<React.ComponentProps<typeof AdminMenu>> = {}) {
  const props: React.ComponentProps<typeof AdminMenu> = {
    loggingOut: false,
    logoutError: "",
    onLogout: vi.fn(),
    onNavigateToActivity: vi.fn(event => event.preventDefault()),
    role: "Administrator",
    subject: "admin",
    ...overrides,
  };
  render(<AdminMenu {...props}/>);
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
  expect(within(actions).getByRole("link", {name: "Open Activity"})).toHaveFocus();
  expect(actions).toHaveClass("admin-menu-panel");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("supports disclosure keyboard navigation and restores trigger focus on Escape", async () => {
  const user = userEvent.setup();
  renderMenu();
  const trigger = screen.getByRole("button", {name: /admin/i});
  await user.click(trigger);

  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("button", {name: "Logout"})).toHaveFocus();
  await user.keyboard("{ArrowUp}");
  expect(screen.getByRole("link", {name: "Open Activity"})).toHaveFocus();
  await user.keyboard("{Escape}");

  expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("closes the menu before navigating to the top-level Activity page", async () => {
  const user = userEvent.setup();
  const onNavigateToActivity = vi.fn(event => event.preventDefault());
  renderMenu({onNavigateToActivity});

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("link", {name: "Open Activity"}));

  expect(onNavigateToActivity).toHaveBeenCalledOnce();
  expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("closes on outside interaction", async () => {
  const user = userEvent.setup();
  const onNavigateToActivity = vi.fn(event => event.preventDefault());
  render(<div>
    <button type="button">Outside</button>
    <AdminMenu loggingOut={false} logoutError="" onLogout={() => undefined} onNavigateToActivity={onNavigateToActivity} role="Administrator" subject="admin"/>
  </div>);

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("button", {name: "Outside"}));
  await waitFor(() => expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument());
});

test("closes and disables all operator actions while global navigation is locked", async () => {
  const user = userEvent.setup();
  const onLogout = vi.fn();
  const onNavigateToActivity = vi.fn(event => event.preventDefault());
  const props = {loggingOut: false, logoutError: "", onLogout, onNavigateToActivity, role: "Administrator", subject: "admin"};
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
  expect(onNavigateToActivity).not.toHaveBeenCalled();
});

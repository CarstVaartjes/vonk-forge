import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {AdminMenu} from "./admin-menu";

function renderMenu(overrides: Partial<React.ComponentProps<typeof AdminMenu>> = {}) {
  const props: React.ComponentProps<typeof AdminMenu> = {
    environment: "Development",
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

test("opens a viewport-safe operator menu and moves focus to its first action", async () => {
  const user = userEvent.setup();
  renderMenu();

  const trigger = screen.getByRole("button", {name: /admin/i});
  expect(trigger).toHaveAttribute("aria-haspopup", "menu");
  await user.click(trigger);

  const menu = screen.getByRole("menu", {name: "Operator menu"});
  expect(within(menu).getByRole("menuitem", {name: "Open Activity"})).toHaveFocus();
  expect(menu).toHaveClass("admin-menu-panel");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("supports menu keyboard navigation and restores trigger focus on Escape", async () => {
  const user = userEvent.setup();
  renderMenu();
  const trigger = screen.getByRole("button", {name: /admin/i});
  await user.click(trigger);

  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", {name: "Logout"})).toHaveFocus();
  await user.keyboard("{ArrowUp}");
  expect(screen.getByRole("menuitem", {name: "Open Activity"})).toHaveFocus();
  await user.keyboard("{Escape}");

  expect(screen.queryByRole("menu", {name: "Operator menu"})).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("closes the menu before navigating to the top-level Activity page", async () => {
  const user = userEvent.setup();
  const onNavigateToActivity = vi.fn(event => event.preventDefault());
  renderMenu({onNavigateToActivity});

  await user.click(screen.getByRole("button", {name: /admin/i}));
  await user.click(screen.getByRole("menuitem", {name: "Open Activity"}));

  expect(onNavigateToActivity).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menu", {name: "Operator menu"})).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("closes on outside interaction and disables Activity navigation during a locked operation", async () => {
  const user = userEvent.setup();
  const onNavigateToActivity = vi.fn(event => event.preventDefault());
  render(<div>
    <button type="button">Outside</button>
    <AdminMenu environment="Development" loggingOut={false} logoutError="" navigationLocked onLogout={() => undefined} onNavigateToActivity={onNavigateToActivity} role="Administrator" subject="admin"/>
  </div>);

  await user.click(screen.getByRole("button", {name: /admin/i}));
  const activity = screen.getByRole("menuitem", {name: "Open Activity"});
  expect(activity).toHaveAttribute("aria-disabled", "true");
  expect(activity).toHaveAttribute("tabindex", "-1");
  await user.click(activity);
  expect(onNavigateToActivity).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", {name: "Outside"}));
  await waitFor(() => expect(screen.queryByRole("menu", {name: "Operator menu"})).not.toBeInTheDocument());
});

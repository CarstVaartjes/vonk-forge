import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {EmptyState} from "./empty-state";

test("an empty list offers its one primary action", async () => {
  const onClick = vi.fn();
  render(<EmptyState title="No things" description="Things are what you make." action={{label: "Make a thing", onClick}}/>);
  await userEvent.click(screen.getByRole("button", {name: "Make a thing"}));
  expect(onClick).toHaveBeenCalledOnce();
});

test("filters that caused the emptiness offer to be cleared instead", async () => {
  const onClear = vi.fn(), onClick = vi.fn();
  render(<EmptyState title="No things" description="d" filtered onClearFilters={onClear} action={{label: "Make a thing", onClick}}/>);
  expect(screen.queryByRole("button", {name: "Make a thing"})).toBeNull();
  await userEvent.click(screen.getByRole("button", {name: "Clear filters"}));
  expect(onClear).toHaveBeenCalledOnce();
});

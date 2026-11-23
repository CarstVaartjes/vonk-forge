import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeToggle } from "./theme-toggle";

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("the toggle switches the theme and names the next one", async () => {
  render(<ThemeToggle />);
  await userEvent.click(screen.getByRole("button", { name: "Theme: dark. Switch to light" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(screen.getByRole("button", { name: "Theme: light. Switch to dark" })).toBeVisible();
});

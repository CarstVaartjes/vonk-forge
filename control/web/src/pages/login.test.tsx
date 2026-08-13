import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {LoginPage} from "./login";

it.each([
  [401, "Sign in failed. Check your credentials and try again."],
  [429, "Sign in is temporarily unavailable. Please try again."],
])("uses a bounded non-secret message for login HTTP %i", async (status, message) => {
  // Break caught: the browser reflects credential/API details, or leaves the
  // submitted password resident after a failed administrator login.
  const login = vi.fn(async () => { throw Object.assign(new Error("synthetic credential detail"), {status}); });
  const user = userEvent.setup();
  render(<LoginPage onLogin={login}/>);

  const password = screen.getByLabelText("Password");
  await user.type(password, "synthetic-test-password");
  await user.click(screen.getByRole("button", {name: "Sign in"}));

  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(screen.getByLabelText("Password")).toHaveValue("");
  expect(screen.getByRole("button", {name: "Sign in"})).toBeDisabled();
  expect(login).toHaveBeenCalledWith("admin", "synthetic-test-password");
  expect(screen.queryByText("synthetic credential detail")).not.toBeInTheDocument();
});

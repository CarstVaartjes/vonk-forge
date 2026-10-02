import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {CancelOperation} from "./cancel-operation";
import {ToastProvider} from "./toast";

test("an accepted cancellation says requested and stays cancelling until the owner observes it stop", async () => {
  const user = userEvent.setup();
  const cancel = vi.fn(async () => ({state: "cancelling"}));
  render(<ToastProvider><CancelOperation what="load" consequence="Stops it." cancel={cancel}/></ToastProvider>);
  await user.click(screen.getByRole("button", {name: "Cancel load"}));
  await user.click(screen.getByRole("button", {name: "Confirm cancel load"}));
  expect(await screen.findByText(/Cancellation requested for the load/)).toBeVisible();
  expect(screen.queryByText(/Cancelled the load/)).toBeNull();
  expect(screen.getByRole("button", {name: "Cancelling load…"})).toBeDisabled();
});

test("a cancellation that is already terminal says cancelled", async () => {
  const user = userEvent.setup();
  render(<ToastProvider><CancelOperation what="load" consequence="Stops it." cancel={async () => ({state: "cancelled"})}/></ToastProvider>);
  await user.click(screen.getByRole("button", {name: "Cancel load"}));
  await user.click(screen.getByRole("button", {name: "Confirm cancel load"}));
  expect(await screen.findByText(/Cancelled the load/)).toBeVisible();
});

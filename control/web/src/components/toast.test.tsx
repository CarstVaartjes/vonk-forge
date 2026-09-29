import {act, render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {ToastProvider, useToast} from "./toast";

function Buttons() {
  const toast = useToast();
  return <button type="button" onClick={() => toast.success("Saved it")}>Go</button>;
}

test("a toast is announced in a live region and dismisses itself unless hovered", async () => {
  vi.useFakeTimers();
  try {
    render(<ToastProvider><Buttons/></ToastProvider>);
    const region = screen.getByRole("region", {name: "Notifications"});
    expect(region).toHaveAttribute("aria-live");
    act(() => screen.getByRole("button", {name: "Go"}).click());
    const toast = within(region).getByText("Saved it");
    act(() => { toast.closest("li")!.dispatchEvent(new MouseEvent("mouseover", {bubbles: true})); });
    // React's onMouseEnter listens to mouseover on the root.
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(within(region).queryByText("Saved it")).not.toBeNull();
    act(() => { toast.closest("li")!.dispatchEvent(new MouseEvent("mouseout", {bubbles: true, relatedTarget: document.body})); });
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(within(region).queryByText("Saved it")).toBeNull();
  } finally { vi.useRealTimers(); }
});

test("a toast can be dismissed by keyboard", async () => {
  const user = userEvent.setup();
  render(<ToastProvider><Buttons/></ToastProvider>);
  await user.click(screen.getByRole("button", {name: "Go"}));
  await user.click(screen.getByRole("button", {name: "Dismiss notification"}));
  expect(screen.queryByText("Saved it")).toBeNull();
});

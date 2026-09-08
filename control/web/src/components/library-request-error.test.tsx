import {fireEvent, render, screen} from "@testing-library/react";
import {ApiError} from "../api/client";
import {LibraryRequestError} from "./library-request-error";

test("shows sanitized API exception text with a local retry action", () => {
  const retry = vi.fn();
  render(<LibraryRequestError error={new ApiError(503, "Controller unavailable. Authorization: Bearer secret-value https://cdn.example.test/file?token=private")} title="Availability status could not be loaded." onRetry={retry} retryLabel="Refresh availability status"/>);
  expect(screen.getByRole("alert")).toHaveTextContent("Controller unavailable. Authorization: Bearer <redacted> <signed-url-redacted>");
  expect(screen.getByRole("alert")).not.toHaveTextContent("secret-value");
  expect(screen.queryByText("Technical details")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name: "Refresh availability status"}));
  expect(retry).toHaveBeenCalledOnce();
});

test("does not interpret arbitrary thrown objects as availability evidence", () => {
  render(<LibraryRequestError error={{failure: {code: "made-up", detail: "Do not display this as evidence", logs: "secret"}}} title="Request failed."/>);
  expect(screen.getByRole("alert")).toHaveTextContent("Request failed.");
  expect(screen.queryByText(/made-up|Do not display|secret|Technical details/)).not.toBeInTheDocument();
});

test("bounds long request exception messages", () => {
  render(<LibraryRequestError error={new Error("failure ".repeat(200))} title="Request failed."/>);
  expect(screen.getByRole("alert").querySelector("p")!.textContent!.length).toBeLessThanOrEqual(512);
  expect(screen.getByRole("alert")).toHaveTextContent("…<truncated>");
});

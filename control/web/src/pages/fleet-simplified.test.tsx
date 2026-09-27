import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach} from "vitest";
import {FleetPage} from "./fleet-simplified";
import type {ControlApi, EnrollmentGrantResponse} from "../api/types";

vi.mock("../hooks/use-fleet-stream", () => ({
  useFleetStream: () => ({
    snapshot: {nodes: []}, now: new Date("2026-09-27T10:00:00Z"), loading: false,
    error: null, retry: vi.fn(),
  }),
}));

const originalClipboard = navigator.clipboard;
afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "clipboard", {configurable: true, value: originalClipboard});
});

const grant: EnrollmentGrantResponse = {
  id: "12345678-1234-4234-8234-123456789abc",
  expires_at: "2026-09-27T10:15:00Z",
  purpose: "new-node",
  token: "t".repeat(43),
  controller_endpoint: "https://controller.example.test",
  enrollment_endpoint: "https://controller.example.test/agent/enroll",
  ca_fingerprint: "a".repeat(64),
  controller_address: "192.168.1.231",
  service_hostnames: [],
  installer_url: "https://install.vonkforge.ai/spark",
};

test("Fleet creates a one-use grant and shows the exact Spark command, token, and expiry", async () => {
  const enrollFleetNode = vi.fn().mockResolvedValue({action: "enroll", state: "created", grant});
  vi.stubGlobal("crypto", {randomUUID: () => "12345678-1234-4234-8234-123456789abc"});
  const userClipboard = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText: userClipboard}});
  render(<FleetPage api={{visualFleet: vi.fn(), enrollFleetNode} as unknown as ControlApi}/>);

  fireEvent.click(screen.getByRole("button", {name: "Enroll Spark"}));
  fireEvent.change(screen.getByLabelText("Spark name"), {target: {value: "Kitchen Spark"}});
  fireEvent.click(screen.getByRole("button", {name: "Create enrollment grant"}));

  await waitFor(() => expect(enrollFleetNode).toHaveBeenCalledWith({
    name: "Kitchen Spark", request_key: "12345678-1234-4234-8234-123456789abc", ttl_seconds: 900,
  }));
  expect(await screen.findByText("Run this command on the Spark. The installer will ask for the one-use pairing token.")).toBeVisible();
  const command = "curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS='192.168.1.231' sh -s -- --enroll";
  expect(screen.getByText(command)).toBeVisible();
  expect(screen.getByLabelText("One-use pairing token")).toHaveValue(grant.token);
  expect(screen.getByText(new Date(grant.expires_at).toLocaleString())).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Copy command"}));
  await waitFor(() => expect(userClipboard).toHaveBeenCalledWith(command));
});

test("Fleet reports grant creation failures without losing the form", async () => {
  const enrollFleetNode = vi.fn().mockRejectedValue(new Error("Control API returned 503: enrollment unavailable"));
  render(<FleetPage api={{visualFleet: vi.fn(), enrollFleetNode} as unknown as ControlApi}/>);
  fireEvent.click(screen.getByRole("button", {name: "Enroll Spark"}));
  fireEvent.change(screen.getByLabelText("Spark name"), {target: {value: "Kitchen Spark"}});
  fireEvent.click(screen.getByRole("button", {name: "Create enrollment grant"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("enrollment unavailable");
  expect(screen.getByLabelText("Spark name")).toHaveValue("Kitchen Spark");
});

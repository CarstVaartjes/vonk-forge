import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {afterEach} from "vitest";
import {FleetPage} from "./fleet-simplified";
import type {ControlApi, EnrollmentGrantResponse} from "../api/types";

let nodes: unknown[] = [];
vi.mock("../hooks/use-fleet-stream", () => ({
  useFleetStream: () => ({
    snapshot: {nodes}, now: new Date("2026-09-27T10:00:00Z"), loading: false,
    error: null, retry: vi.fn(),
  }),
}));

function spark(name: string, memoryFree: number, diskFree: number, online = true) {
  return {
    id: `spk_${name.padEnd(32, "0")}`, display_name: name, hostname: `${name.toLowerCase()}.local`, labels: {},
    connection: {online_state: online ? "online" : "offline", offline_reason: online ? null : "stale", last_seen_at: null},
    inventory: {disk_free_bytes: diskFree, disk_total_bytes: 1000, host_memory_free_bytes: memoryFree, host_memory_total_bytes: 100},
    telemetry: null, installed: [], loaded: [], warnings: [],
  };
}

function order(): string[] {
  return screen.getAllByRole("row").slice(1).map(row => within(row).getAllByRole("link")[0]!.textContent ?? "");
}

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
  enrollment_endpoint: "https://enroll.example.test",
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
  render(<FleetPage api={{visualFleet: vi.fn(), enrollFleetNode, enrollmentStatus: vi.fn().mockResolvedValue({state: "pending"})} as unknown as ControlApi}/>);

  fireEvent.click(screen.getByRole("button", {name: "Enroll Spark"}));
  fireEvent.change(screen.getByLabelText("Spark name"), {target: {value: "Kitchen Spark"}});
  fireEvent.click(screen.getByRole("button", {name: "Create enrollment grant"}));

  await waitFor(() => expect(enrollFleetNode).toHaveBeenCalledWith({
    name: "Kitchen Spark", request_key: "12345678-1234-4234-8234-123456789abc",
  }));
  expect(await screen.findByText("Run this command on the Spark. The installer will ask for the one-use pairing token.")).toBeVisible();
  const command = `curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS='192.168.1.231' VONK_ENROLLMENT_URL='https://enroll.example.test' VONK_CONTROLLER_CA_SHA256='${"a".repeat(64)}' sh -s -- --enroll`;
  expect(screen.getByText(command)).toBeVisible();
  expect(screen.getByLabelText("One-use pairing token")).toHaveValue(grant.token);
  expect(screen.getByText(/Grant expires/).closest("p")!.querySelector("time")).toHaveAttribute("dateTime", grant.expires_at);
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

test("Fleet sorts by name, status, memory and disk, and uses the CLI's status words", () => {
  nodes = [spark("Bravo", 20, 500), spark("Alpha", 60, 100, false), spark("Charlie", 90, 900)];
  render(<FleetPage api={{} as ControlApi}/>);
  expect(order()).toEqual(["Alpha", "Bravo", "Charlie"]);
  fireEvent.click(screen.getByRole("button", {name: /Status/}));
  expect(order()[0]).toBe("Alpha");
  fireEvent.click(screen.getByRole("button", {name: /Memory used/}));
  expect(order()).toEqual(["Charlie", "Alpha", "Bravo"]);
  fireEvent.click(screen.getByRole("button", {name: /Memory used/}));
  expect(order()).toEqual(["Bravo", "Alpha", "Charlie"]);
  fireEvent.click(screen.getByRole("button", {name: /Disk free/}));
  expect(order()).toEqual(["Alpha", "Bravo", "Charlie"]);
  expect(screen.getAllByText("offline")).toHaveLength(1);
  expect(screen.getAllByText("online")).toHaveLength(2);
  nodes = [];
});

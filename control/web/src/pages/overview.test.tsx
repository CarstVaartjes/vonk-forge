vi.mock("../components/metrics-chart", () => ({ MetricsChart: () => null }));
import { readFileSync } from "node:fs";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach } from "vitest";
import { OverviewPage } from "./overview";
import { ProjectionCode, ProfileReasonSeverity } from "../api/vocabulary.generated";
import { ToastProvider } from "../components/toast";
import type { ControlApi, EnrollmentGrantResponse } from "../api/types";
import { spark } from "../test-fixtures/fleet-spark";

let nodes: unknown[] = [];
let attention: unknown[] = [];
let attentionUnavailable = false;
let fleetNow = new Date("2026-09-27T10:00:00Z");
vi.mock("../hooks/use-fleet-stream", () => ({
  useFleetStream: () => ({
    snapshot: { nodes, attention, attention_unavailable: attentionUnavailable },
    now: fleetNow,
    loading: false,
    error: null,
    retry: vi.fn(),
  }),
}));

function order(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("link")[0]!.textContent ?? "");
}

const originalClipboard = navigator.clipboard;
afterEach(() => {
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: originalClipboard });
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

const api = {
  metricsSeries: () => Promise.reject(new Error("no history")),
} as unknown as ControlApi;

test("Fleet creates a one-use grant and shows the exact Spark command, token, and expiry", async () => {
  const enrollFleetNode = vi.fn().mockResolvedValue({ action: "enroll", state: "created", grant });
  vi.stubGlobal("crypto", { randomUUID: () => "12345678-1234-4234-8234-123456789abc" });
  const userClipboard = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: userClipboard },
  });
  render(
    <OverviewPage
      api={
        {
          visualFleet: vi.fn(),
          enrollFleetNode,
          enrollmentStatus: vi.fn().mockResolvedValue({ state: "pending" }),
        } as unknown as ControlApi
      }
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "Enroll Spark" }));
  fireEvent.change(screen.getByLabelText("Spark name"), { target: { value: "Kitchen Spark" } });
  fireEvent.click(screen.getByRole("button", { name: "Create enrollment grant" }));

  await waitFor(() =>
    expect(enrollFleetNode).toHaveBeenCalledWith({
      name: "Kitchen Spark",
      request_key: "12345678-1234-4234-8234-123456789abc",
    }),
  );
  expect(
    await screen.findByText(
      "Run this command on the Spark. The installer will ask for the one-use pairing token.",
    ),
  ).toBeVisible();
  const command = `curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS='192.168.1.231' VONK_ENROLLMENT_URL='https://enroll.example.test' VONK_CONTROLLER_CA_SHA256='${"a".repeat(64)}' sh -s -- --enroll`;
  expect(screen.getByText(command)).toBeVisible();
  expect(screen.getByLabelText("One-use pairing token")).toHaveValue(grant.token);
  expect(
    screen
      .getByText(/Grant expires/)
      .closest("p")!
      .querySelector("time"),
  ).toHaveAttribute("dateTime", grant.expires_at);
  fireEvent.click(screen.getByRole("button", { name: "Copy command" }));
  await waitFor(() => expect(userClipboard).toHaveBeenCalledWith(command));
});

test("Fleet reports grant creation failures without losing the form", async () => {
  const enrollFleetNode = vi
    .fn()
    .mockRejectedValue(new Error("Control API returned 503: enrollment unavailable"));
  render(
    <ToastProvider>
      <OverviewPage api={{ visualFleet: vi.fn(), enrollFleetNode } as unknown as ControlApi} />
    </ToastProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Enroll Spark" }));
  fireEvent.change(screen.getByLabelText("Spark name"), { target: { value: "Kitchen Spark" } });
  fireEvent.click(screen.getByRole("button", { name: "Create enrollment grant" }));
  expect(
    await within(screen.getByRole("region", { name: "Notifications" })).findByText(
      /enrollment unavailable/,
    ),
  ).toBeVisible();
  expect(screen.getByLabelText("Spark name")).toHaveValue("Kitchen Spark");
});

test("Fleet status line reads exactly what `vonkctl fleet` prints for the shared vectors", () => {
  const vectors = JSON.parse(
    readFileSync("../../tests/fixtures/fleet-wording-status.json", "utf8"),
  ) as {
    now: string;
    cases: {
      nodes: Record<string, unknown>[];
      attention?: unknown[];
      attention_unavailable?: boolean;
      expected: string;
    }[];
  };
  fleetNow = new Date(vectors.now);
  for (const vector of vectors.cases) {
    nodes = vector.nodes.map((node) => ({ ...spark("Alpha", 40, 250), ...node }));
    attention = vector.attention ?? [];
    attentionUnavailable = vector.attention_unavailable ?? false;
    const { container, unmount } = render(<OverviewPage api={api} />);
    expect(container.querySelector(".fleet-status-line")?.textContent).toBe(vector.expected);
    expect(container.querySelector(".fleet-status-line")?.getAttribute("role")).toBeNull();
    unmount();
  }
  fleetNow = new Date("2026-09-27T10:00:00Z");
  attentionUnavailable = false;
  nodes = [];
  attention = [];
});

test("firing alerts read exactly what `vonkctl fleet` prints for the shared vectors", () => {
  const vectors = JSON.parse(
    readFileSync("../../tests/fixtures/fleet-wording-attention.json", "utf8"),
  ) as {
    nodes: { id: string; display_name: string }[];
    alerts: unknown[];
    expected: string[];
    unavailable: string;
  };
  nodes = vectors.nodes.map((node) => ({ ...spark("Alpha", 40, 250), ...node }));
  attention = vectors.alerts;
  const { container, unmount } = render(<OverviewPage api={api} />);
  const region = screen.getByRole("region", { name: "Needs attention" });
  expect(
    within(region)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual(vectors.expected);
  expect(within(region).queryByRole("button")).toBeNull();
  expect(container.textContent).not.toContain("NodeHot");
  unmount();
  attention = [];
  attentionUnavailable = true;
  render(<OverviewPage api={api} />);
  expect(screen.getByText(vectors.unavailable)).toBeVisible();
  attentionUnavailable = false;
  nodes = [];
});

test("Fleet shows the Controller headless memory recommendation without an apply action", () => {
  // Break caught: dropping non-blocking advice from the Fleet overview.
  const detail = "Headless Spark display reserves 2147483648 bytes; minimum is 0.5 GB.";
  const recommendation =
    "Update SoC firmware (fwupdmgr update), then set Advanced → Advanced Menu → Display Reserved Memory to 0.5 GB (fixed); save and reboot.";
  nodes = [
    {
      ...spark("Alpha", 60, 100),
      warnings: [
        {
          code: ProjectionCode.HOST_DISPLAY_MEMORY,
          detail,
          recommendation,
          severity: ProfileReasonSeverity.WARNING,
        },
      ],
    },
  ];
  attention = [];
  render(<OverviewPage api={api} />);
  expect(screen.getByText(`${detail} ${recommendation}`)).toBeVisible();
});

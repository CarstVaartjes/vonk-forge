vi.mock("../components/ui/metrics-chart", () => ({ MetricsChart: () => null }));
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach } from "vitest";
import { OverviewPage } from "./overview";
import { ProjectionCode, ProfileReasonSeverity } from "../api/vocabulary.generated";
import { ToastProvider } from "../components/ui/toast";
import type { ControlApi, EnrollmentGrantResponse } from "../api/types";
import { spark } from "../test-fixtures/fleet-spark";

let nodes: unknown[] = [];
let attention: unknown[] = [];
const attentionUnavailable = false;
const fleetNow = new Date("2026-09-27T10:00:00Z");
vi.mock("../hooks/use-fleet-stream", () => ({
  useFleetStream: () => ({
    snapshot: { nodes, attention, attention_unavailable: attentionUnavailable },
    now: fleetNow,
    loading: false,
    error: null,
    retry: vi.fn(),
  }),
}));

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
  controller_address: "192.0.2.231",
  service_hostnames: [],
  installer_url: "https://install.vonkforge.ai/spark",
};

const api = {
  metricsSeries: () => Promise.reject(new Error("no history")),
  operations: () => Promise.reject(new Error("no activity")),
  metricsCatalogue: () => Promise.resolve([]),
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
          metricsSeries: api.metricsSeries,
          metricsCatalogue: api.metricsCatalogue,
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
  const command = `curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS='192.0.2.231' VONK_ENROLLMENT_URL='https://enroll.example.test' VONK_CONTROLLER_CA_SHA256='${"a".repeat(64)}' sh -s -- --enroll`;
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
      <OverviewPage
        api={
          {
            visualFleet: vi.fn(),
            metricsSeries: api.metricsSeries,
            metricsCatalogue: api.metricsCatalogue,
            enrollFleetNode,
          } as unknown as ControlApi
        }
      />
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

test("Upgrade agents is disabled with no Sparks enrolled and enabled once one is", () => {
  nodes = [];
  const { unmount } = render(<OverviewPage api={api} />);
  expect(screen.getByRole("button", { name: "Upgrade agents" })).toBeDisabled();
  unmount();
  nodes = [spark("Alpha", 40, 250)];
  render(<OverviewPage api={api} />);
  expect(screen.getByRole("button", { name: "Upgrade agents" })).toBeEnabled();
  nodes = [];
});

test("Upgrade agents is offered only while some Spark has an upgrade to take", () => {
  nodes = [
    { ...spark("Alpha", 40, 250), agent_upgrade_available: false },
    { ...spark("Beta", 40, 250), agent_upgrade_available: false },
  ];
  const { unmount } = render(<OverviewPage api={api} />);
  expect(screen.getByRole("button", { name: "Upgrade agents" })).toBeDisabled();
  unmount();
  // One that can take it, or one the Controller cannot tell yet, keeps it available.
  nodes = [
    { ...spark("Alpha", 40, 250), agent_upgrade_available: false },
    { ...spark("Beta", 40, 250), agent_upgrade_available: true },
  ];
  render(<OverviewPage api={api} />);
  expect(screen.getByRole("button", { name: "Upgrade agents" })).toBeEnabled();
  nodes = [];
});

test("the upgrade notice links to Activity instead of printing the operation id", async () => {
  nodes = [spark("Alpha", 40, 250)];
  const upgradeFleet = vi
    .fn()
    .mockResolvedValue({ state: "accepted", operation_id: "11111111-2222-4333-8444-555555555555" });
  const onNavigate = vi.fn((event: { preventDefault(): void }) => event.preventDefault());
  render(
    <ToastProvider>
      <OverviewPage
        api={{ ...api, upgradeFleet } as unknown as ControlApi}
        onNavigate={onNavigate}
      />
    </ToastProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Upgrade agents" }));
  const dialog = screen.getByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Upgrade agents" }));
  const link = await screen.findByRole("link", { name: "Follow it in Activity" });
  expect(link).toHaveAttribute("href", "/activity");
  expect(screen.getByRole("region", { name: "Notifications" }).textContent).not.toContain(
    "11111111-2222",
  );
  fireEvent.click(link);
  expect(onNavigate).toHaveBeenCalledWith(expect.anything(), "/activity");
  nodes = [];
});

test("the status line is joined by a link to the unsuccessful operations the Activity rail read", async () => {
  // Break caught: the banner claims all is well while the Activity rail lists an unsuccessful operation.
  nodes = [spark("Alpha", 40, 250)];
  const onNavigate = vi.fn();
  const operations = async () => ({
    total: 1,
    next_cursor: null,
    operations: [
      {
        id: "op-1",
        kind: "recipe.start",
        state: "failed",
        summary: "Recipe start failed",
        created_at: "2026-09-27T08:00:00Z",
        node_ids: [],
      },
    ],
  });
  render(
    <ToastProvider>
      <OverviewPage api={{ ...api, operations } as unknown as ControlApi} onNavigate={onNavigate} />
    </ToastProvider>,
  );
  const link = await screen.findByRole("link", { name: "1 unsuccessful" });
  expect(link).toHaveAttribute("href", "/activity?status=unsuccessful");
  fireEvent.click(link);
  expect(onNavigate).toHaveBeenCalledWith(expect.anything(), "/activity?status=unsuccessful");
  nodes = [];
});

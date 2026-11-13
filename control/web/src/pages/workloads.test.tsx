vi.mock("../components/metrics-chart", () => ({ MetricsChart: () => null }));
import { readFileSync } from "node:fs";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { WorkloadsPage } from "./workloads";
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

// Series by metric; a metric not listed fails to read, which a figure shows as Not reported.
let reads: Record<string, unknown> = {};
const api = {
  metricsSeries: (metric: string) =>
    metric in reads ? Promise.resolve(reads[metric]) : Promise.reject(new Error("no history")),
  operations: () => Promise.reject(new Error("no activity")),
  metricsCatalogue: () => Promise.resolve([]),
} as unknown as ControlApi;

function series(metric: string, unit: string, latest: number, total: number | null = null) {
  return {
    metric,
    spec: {
      metric,
      title: `${metric} title`,
      unit,
      scope: "alias",
      basis: "measured",
      source: "gateway",
      summary_total: true,
      fleet_sum: false,
      hint: "",
    },
    range: "1h",
    start: 0,
    end: 3600,
    step_seconds: 15,
    series: [
      {
        labels: { alias: "coder" },
        points: [3000, 3300, 3600].map((timestamp) => ({ timestamp, value: latest })),
        summary: { latest, average: latest, peak: latest, total, share: null, coverage: 1 },
      },
    ],
  };
}

function run(state: string, members: string[], rank: number) {
  return {
    run_id: "run-1",
    title: "GLM 5.3 Flash",
    alias: "coder",
    rank,
    run_state: state,
    healthy: true,
    member_node_ids: members,
  };
}

function order(): string[] {
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("link")[0]!.textContent ?? "");
}

beforeEach(() => {
  nodes = [];
  attention = [];
  reads = {};
});

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

test("Fleet sorts by name, status, memory and disk, and uses the CLI's status words", () => {
  nodes = [spark("Bravo", 20, 500), spark("Alpha", 60, 100, false), spark("Charlie", 90, 900)];
  render(<WorkloadsPage api={api} />);
  expect(order()).toEqual(["Alpha", "Bravo", "Charlie"]);
  fireEvent.click(screen.getByRole("button", { name: /Status/ }));
  expect(order()[0]).toBe("Alpha");
  fireEvent.click(screen.getByRole("button", { name: /Memory used/ }));
  expect(order()).toEqual(["Charlie", "Alpha", "Bravo"]);
  fireEvent.click(screen.getByRole("button", { name: /Memory used/ }));
  expect(order()).toEqual(["Bravo", "Alpha", "Charlie"]);
  fireEvent.click(screen.getByRole("button", { name: /Disk free/ }));
  expect(order()).toEqual(["Alpha", "Bravo", "Charlie"]);
  expect(screen.getAllByText("offline")).toHaveLength(1);
  expect(screen.getAllByText("online")).toHaveLength(2);
  nodes = [];
});

test("Fleet vitals cells read exactly what `vonkctl fleet` prints for the shared vectors", () => {
  const vectors = JSON.parse(
    readFileSync("../../tests/fixtures/fleet-wording-vitals.json", "utf8"),
  ) as {
    cases: {
      node: Record<string, unknown>;
      expected: { memory: string; disk: string; gpu: string };
    }[];
  };
  for (const { node, expected } of vectors.cases) {
    nodes = [{ ...spark("Alpha", 40, 250), ...node }];
    const { container, unmount } = render(<WorkloadsPage api={api} />);
    const cell = (label: string) =>
      container.querySelector(`td[data-label="${label}"]`)?.textContent;
    expect(cell("Memory used")).toBe(expected.memory);
    expect(cell("Disk free")).toBe(expected.disk);
    expect(cell("GPU")).toBe(expected.gpu);
    unmount();
  }
  nodes = [];
});

test("Fleet meters turn amber for low free memory and full disk, never for GPU utilisation", () => {
  nodes = [
    {
      ...spark("Alpha", 5, 50),
      telemetry: {
        freshness: "live",
        sample: {
          observed_at: "2026-09-27T10:00:00Z",
          memory_total_bytes: 100,
          memory_available_bytes: 5,
          gpu_utilization_percent: 99,
        },
      },
    },
  ];
  render(<WorkloadsPage api={api} />);
  const [memory, disk, gpu] = within(screen.getAllByRole("row")[1]!).getAllByRole("meter");
  expect(memory!.className).toContain("meter-high");
  expect(disk!.className).toContain("meter-high");
  expect(gpu!.className).not.toContain("meter-high");
  expect(disk!.getAttribute("aria-label")).toBe("Alpha disk free");
  nodes = [];
});

test("Fleet Serving cells read exactly what `vonkctl fleet` prints for the shared vectors", () => {
  const vectors = JSON.parse(
    readFileSync("../../tests/fixtures/fleet-wording-workloads.json", "utf8"),
  ) as {
    nodes: { id: string; display_name: string }[];
    expected: Record<string, { page: string[] }>;
  };
  nodes = vectors.nodes.map((node) => ({ ...spark("Alpha", 40, 250), ...node }));
  const { container } = render(<WorkloadsPage api={api} />);
  for (const node of vectors.nodes) {
    const row = [...container.querySelectorAll("tbody tr")].find((candidate) =>
      candidate.querySelector("th")?.textContent?.includes(node.display_name),
    );
    const lines = [...(row?.querySelectorAll('td[data-label="Serving"] > strong') ?? [])].map(
      (line) => line.textContent,
    );
    expect(lines).toEqual(vectors.expected[node.id]!.page);
  }
  nodes = [];
});

test("a workload is one row with its Sparks, measured speed and requests; opening it shows the unit", async () => {
  reads = {
    requests_running: series("requests_running", "count", 3),
    requests_waiting: series("requests_waiting", "count", 1),
    output_token_rate: series("output_token_rate", "tokens_per_second", 38, 1_840_000),
  };
  const both = ["spk_Alpha", "spk_Bravo"].map((id) => id.padEnd(36, "0"));
  nodes = [
    { ...spark("Alpha", 40, 250), id: both[0], loaded: [run("running", both, 0)] },
    { ...spark("Bravo", 40, 250), id: both[1], loaded: [run("running", both, 1)] },
  ];
  render(<WorkloadsPage api={api} />);
  const toggle = screen.getByRole("button", { name: "GLM 5.3 Flash" });
  const row = toggle.closest("li")!;
  expect(within(row).getByText("coder")).toBeInTheDocument();
  expect(
    within(within(row).getByRole("list", { name: "Sparks" })).getAllByRole("listitem"),
  ).toHaveLength(2);
  // The lead figure is the gateway's counted tokens per second; the queue is the engine's.
  await waitFor(() => expect(row.textContent).toContain("38tok/s"));
  expect(row.textContent).toContain("3 running · 1 waiting");
  expect(row.textContent).toContain("1.84M");
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(within(row).getByLabelText("GLM 5.3 Flash")).toBeInTheDocument();
  expect(within(row).getByLabelText("GLM 5.3 Flash engine")).toBeInTheDocument();
});

test("with no workload loaded the page says so and still lists every Spark", () => {
  nodes = [spark("Alpha", 40, 250)];
  render(<WorkloadsPage api={api} />);
  expect(screen.getByText("Nothing is loaded on any Spark.")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "All Sparks" })).toBeInTheDocument();
  nodes = [];
});

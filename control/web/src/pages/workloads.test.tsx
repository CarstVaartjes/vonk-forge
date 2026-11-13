vi.mock("../components/metrics-chart", () => ({ MetricsChart: () => null }));
import { readFileSync } from "node:fs";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach } from "vitest";
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

test("Fleet sorts by name, status, memory and disk, and uses the CLI's status words", () => {
  nodes = [spark("Bravo", 20, 500), spark("Alpha", 60, 100, false), spark("Charlie", 90, 900)];
  render(<WorkloadsPage api={{} as ControlApi} />);
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
    const { container, unmount } = render(<WorkloadsPage api={{} as ControlApi} />);
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
  render(<WorkloadsPage api={{} as ControlApi} />);
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
  const { container } = render(<WorkloadsPage api={{} as ControlApi} />);
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

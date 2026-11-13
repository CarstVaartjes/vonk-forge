import { render, screen } from "@testing-library/react";
import { WorkloadHero } from "./workload-hero";
import type { MetricsSeriesResponse } from "../api/types";
import type { WorkloadReads } from "../hooks/use-workload-reads";
import type { WorkloadGroup } from "../lib/fleet";

const unit = {
  runId: "r",
  title: "GLM 5.3 Flash",
  alias: "coder",
  state: "running",
  tone: "ok",
  nodes: [],
  ranks: {},
} as WorkloadGroup;

function series(metric: string, title: string, unitName: string, latest: number | null) {
  const points = [100, 160, 200].map((t) => ({ timestamp: t, value: latest }));
  return {
    metric,
    spec: {
      metric,
      title,
      unit: unitName,
      scope: "alias",
      basis: "estimated",
      source: "gateway",
      summary_total: true,
      fleet_sum: false,
      hint: "",
    },
    range: "1h",
    start: 0,
    end: 200,
    step_seconds: 15,
    series: [
      {
        labels: { alias: "coder" },
        points,
        summary: { latest, average: latest, peak: latest, total: null, share: null, coverage: 1 },
      },
    ],
  } as unknown as MetricsSeriesResponse;
}
const tokens = (latest: number | null) =>
  series("output_token_rate", "Generated tokens per model", "tokens_per_second", latest);
const requests = (latest: number | null) =>
  series("request_rate", "Requests per model", "per_second", latest);

// Every read in, none reported, unless a test says otherwise.
const reads = (over: Partial<WorkloadReads>): WorkloadReads => ({
  decode: null,
  tokens: null,
  requests: null,
  running: null,
  waiting: null,
  ...over,
});
const show = (over: Partial<WorkloadReads>) =>
  render(<WorkloadHero unit={unit} reads={reads(over)} live={false} />);

test("the hero is the gateway's generated tokens per second, marked as an estimate", () => {
  const { container } = show({ tokens: tokens(96.7), requests: requests(2.5) });
  expect(container.querySelector(".hero-num")).toHaveTextContent("≈96.7tok/s");
  expect(screen.getByText("Generated tokens per model · last 30 minutes")).toBeInTheDocument();
  expect(screen.getByText("est.")).toBeInTheDocument();
  expect(screen.getByRole("img", { name: /now 96.7 tok\/s/ })).toBeInTheDocument();
});

test("zero traffic on a running workload reads 0 and idle, model loaded", () => {
  const { container } = show({ tokens: tokens(0), requests: requests(0) });
  expect(container.querySelector(".hero-num")).toHaveTextContent("≈0tok/s");
  expect(screen.getByText("idle, model loaded")).toBeInTheDocument();
});

test("without tokens the hero falls back to requests; with none it says Not reported, and draws no chart", () => {
  const { unmount } = show({ requests: requests(2.5) });
  expect(screen.getByText("Requests per model · last 30 minutes")).toBeInTheDocument();
  unmount();
  show({ tokens: tokens(null) });
  expect(screen.getAllByText("Not reported")).toHaveLength(7);
  expect(screen.queryByRole("img")).toBeNull();
});

test("while the speed reads are out the hero claims nothing", () => {
  show({
    decode: undefined,
    tokens: undefined,
    requests: undefined,
    running: undefined,
    waiting: undefined,
  });
  expect(screen.queryByText("Not reported", { selector: ".hero-num" })).toBeNull();
});

test("the engine's measured decode speed leads the gateway's estimate and carries no mark", () => {
  const decode = series("decode_throughput", "Decode throughput", "tokens_per_second", 41.2);
  decode.spec.basis = "measured";
  const { container } = show({ decode, tokens: tokens(96.7), requests: requests(2.5) });
  expect(container.querySelector(".hero-num")).toHaveTextContent("41.2tok/s");
  expect(container.querySelector(".hero-num .estimate-mark")).toBeNull();
});

test("the Requests figure is the engine's running and waiting counts", () => {
  const count = (metric: string, latest: number) => series(metric, metric, "count", latest);
  show({ running: count("requests_running", 3), waiting: count("requests_waiting", 1) });
  expect(screen.getByText("Requests").nextSibling).toHaveTextContent("3 running · 1 waiting");
});

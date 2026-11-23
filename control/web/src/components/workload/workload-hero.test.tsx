import { act, render, screen } from "@testing-library/react";
import { WorkloadHero } from "./workload-hero";
import type {
  ArtifactJob,
  MetricsSeriesResponse,
  RecipeDetail,
  VisualFleetNode,
} from "../../api/types";
import type { JobWorkloadApi } from "../../hooks/use-workload-jobs";
import type { WorkloadReads } from "../../hooks/use-workload-reads";
import { groupByWorkload } from "../../lib/fleet";

// The real projection ids: the run carries a catalog recipe id and the revision id the recipe read takes.
const REVISION = "0b6c0f0e-4f56-4d27-8c0f-2f7a1d5a6a01";
const unit = groupByWorkload([
  {
    id: "spk_0123456789abcdef0123456789abcdef",
    display_name: "Spark One",
    loaded: [
      {
        run_id: "r",
        recipe_id: "6a5b4c3d-0000-4000-8000-000000000001",
        recipe_revision_id: REVISION,
        title: "GLM 5.3 Flash",
        alias: "coder",
        run_state: "running",
        healthy: true,
        rank: 0,
      },
    ],
  } as unknown as VisualFleetNode,
]).clusters[0]!.units[0]!;

function series(metric: string, title: string, unitName: string, latest: number | null) {
  const points = [100, 160, 200].map((t) => ({ timestamp: t, value: latest }));
  return {
    metric,
    spec: {
      metric,
      title,
      unit: unitName,
      scope: "alias",
      basis: "measured",
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
  tokens: null,
  requests: null,
  running: null,
  waiting: null,
  figures: [],
  specs: [],
  ...over,
});
const show = (over: Partial<WorkloadReads>) =>
  render(<WorkloadHero unit={unit} reads={reads(over)} live={false} />);

test("the hero is the gateway's generated tokens per second", () => {
  const { container } = show({ tokens: tokens(96.7), requests: requests(2.5) });
  expect(container.querySelector(".hero-num")).toHaveTextContent("96.7tok/s");
  expect(screen.getByText("Generated tokens per model · last 30 minutes")).toBeInTheDocument();
  expect(screen.getByRole("img", { name: /now 96.7 tok\/s/ })).toBeInTheDocument();
});

test("zero traffic on a running workload reads 0 and idle, model loaded", () => {
  const { container } = show({ tokens: tokens(0), requests: requests(0) });
  expect(container.querySelector(".hero-num")).toHaveTextContent("0tok/s");
  expect(screen.getByText("idle, model loaded")).toBeInTheDocument();
});

test("without tokens the hero falls back to requests; with none it says Not reported, and draws no chart", () => {
  const { unmount } = show({ requests: requests(2.5) });
  expect(screen.getByText("Requests per model · last 30 minutes")).toBeInTheDocument();
  unmount();
  show({ tokens: tokens(null) });
  expect(screen.getAllByText("Not reported")).toHaveLength(2);
  expect(screen.queryByRole("img")).toBeNull();
});

test("while the speed reads are out the hero claims nothing", () => {
  show({
    tokens: undefined,
    requests: undefined,
    running: undefined,
    waiting: undefined,
  });
  expect(screen.queryByText("Not reported", { selector: ".hero-num" })).toBeNull();
});

test("the Requests figure is the engine's running and waiting counts", () => {
  const count = (metric: string, latest: number) => series(metric, metric, "count", latest);
  show({ running: count("requests_running", 3), waiting: count("requests_waiting", 1) });
  expect(screen.getByText("Requests").nextSibling).toHaveTextContent("3 running · 1 waiting");
});

test("an engine figure takes its title and unit from the catalogue; a metric it does not list is not shown", () => {
  const kv = series("kv_cache_usage", "KV cache in use", "ratio", 0.42);
  kv.spec.basis = "measured";
  show({
    specs: [kv.spec],
    figures: [null, null, kv, null, null],
  });
  expect(screen.getByText("KV cache in use").nextSibling).toHaveTextContent("42.0%");
  expect(screen.queryByText("Prefill throughput")).toBeNull();
});

// Answers only the revision id, as the Controller does: the catalog recipe id is a 404.
class FakeApi {
  reads = 0;
  failures = 0;
  private readonly adapter: string;
  constructor(adapter: string) {
    this.adapter = adapter;
  }
  async recipeDetail(selector: string) {
    this.reads += 1;
    if (selector !== REVISION || this.failures-- > 0) throw new Error("404 recipe not found");
    return {
      identity: { publisher: "vonk", slug: "h3-video" },
      document: { interfaces: [{ adapter: this.adapter }] },
    } as unknown as RecipeDetail;
  }
  async artifactJobsForRun(_run: string, _signal?: AbortSignal, limit?: number) {
    return {
      jobs: [
        {
          id: "j1",
          interface: "video-job",
          state: "succeeded",
          created_at: "2026-11-19T10:00:00Z",
          updated_at: "2026-11-19T10:15:16Z",
        } as ArtifactJob,
      ].slice(0, limit),
    };
  }
}
const mount = (api: FakeApi) =>
  render(<WorkloadHero api={api} unit={unit} reads={reads({})} live={false} />);

test("a job recipe shows its jobs and a create link to the running revision, not engine figures", async () => {
  mount(new FakeApi("video-job"));
  expect(await screen.findByText(/takes jobs, not chat requests/)).toBeInTheDocument();
  expect(await screen.findByText(/15m 16s/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Create a video" })).toHaveAttribute(
    "href",
    `/library/recipes/${REVISION}`,
  );
  expect(screen.queryByText("Requests")).toBeNull();
  expect(screen.queryByText("Not reported")).toBeNull();
});

test("a chat recipe keeps the engine figures", async () => {
  mount(new FakeApi("openai"));
  expect(await screen.findByText("Requests")).toBeInTheDocument();
  expect(screen.queryByText(/takes jobs/)).toBeNull();
});

test("before the first read nothing is claimed; after a failed read the model hero stays, and a retry resolves it", async () => {
  vi.useFakeTimers();
  try {
    const api = new FakeApi("video-job");
    api.failures = 1;
    const view = mount(api);
    expect(screen.getByRole("status")).toHaveTextContent("Reading what this recipe serves");
    expect(view.container.querySelector(".hero-body")).toBeNull();
    await act(async () => {});
    // The read failed: the speed tiles stay, with one retry line.
    expect(screen.getByRole("status")).toHaveTextContent("Recipe unavailable");
    expect(view.container.querySelector(".hero-body")).not.toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByText(/takes jobs, not chat requests/)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});

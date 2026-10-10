vi.mock("./metrics-chart", () => ({MetricsChart: () => null}));
import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {SparkDetail} from "./spark-detail";
import type {ControlApi} from "../api/types";

const node = {
  id: "spk_1", display_name: "Kitchen", hostname: "kitchen.local", ip_address: null, warnings: [], loaded: [], installed: [],
  connection: {online_state: "online", last_seen_at: "2026-09-27T10:00:00Z"},
};

test("removing a Spark requires typing its name", async () => {
  const removeFleetNode = vi.fn().mockResolvedValue({action: "remove", state: "done"});
  const onClose = vi.fn();
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(node), removeFleetNode} as unknown as ControlApi} id="spk_1" onClose={onClose}/>);
  fireEvent.click(await screen.findByRole("tab", {name: "Settings"}));
  fireEvent.click(screen.getByRole("button", {name: "Remove"}));
  const confirm = screen.getByRole("button", {name: "Remove Spark"});
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/Type/), {target: {value: "Kitchen"}});
  fireEvent.click(confirm);
  await waitFor(() => expect(removeFleetNode).toHaveBeenCalledWith("spk_1"));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

test("recent logs load when the Logs tab opens", async () => {
  const fleetLogs = vi.fn().mockResolvedValue({entries: [{evidence_id: "e1", level: "error", source: "runtime", observed_at: "2026-09-27T10:00:00Z", message: "boom"}]});
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(node), fleetLogs} as unknown as ControlApi} id="spk_1" onClose={vi.fn()}/>);
  fireEvent.click(await screen.findByRole("tab", {name: "Logs"}));
  expect(await screen.findByText(/boom/)).toBeVisible();
});

test("a Spark with a Controller warning is shown as needing attention with the reason", async () => {
  const warned = {...node, warnings: [{code: "install.partial", detail: "Qwen is partly installed", severity: "warning"}]};
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(warned)} as unknown as ControlApi} id="spk_1" onClose={vi.fn()}/>);
  expect(await screen.findByText("needs attention")).toBeVisible();
  expect(screen.getByText(/Qwen is partly installed/)).toBeVisible();
});

test("a running workload shows the recipe options it was started with", async () => {
  const running = {...node, loaded: [{run_id: "r1", rank: 0, title: "GLM", healthy: true, option_choices: {verification: "adaptive-k"}}]};
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(running)} as unknown as ControlApi} id="spk_1" onClose={vi.fn()}/>);
  const list = await screen.findByRole("list", {name: "Active recipe options"});
  expect(list).toHaveTextContent("adaptive-k");
});

test("the Overview shows the CPU clock next to the temperature", async () => {
  const measured = {...node, telemetry: {freshness: "live", age_seconds: 1, sample: {cpu_frequency_avg_mhz: 2100, cpu_frequency_max_mhz: 3900, gpu_temperature_c: 84}}};
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(measured)} as unknown as ControlApi} id="spk_1" onClose={vi.fn()}/>);
  expect(await screen.findByText(/2\.1 of 3\.9 GHz/)).toBeVisible();
  expect(screen.getByText(/84 °C/)).toBeVisible();
});

test("the Overview omits the CPU clock when the agent does not report it", async () => {
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(node)} as unknown as ControlApi} id="spk_1" onClose={vi.fn()}/>);
  await screen.findByText("Kitchen");
  expect(screen.queryByText("CPU clock")).toBeNull();
});

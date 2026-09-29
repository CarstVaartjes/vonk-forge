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
  fireEvent.click(await screen.findByRole("button", {name: "Remove"}));
  const confirm = screen.getByRole("button", {name: "Remove Spark"});
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/Type/), {target: {value: "Kitchen"}});
  fireEvent.click(confirm);
  await waitFor(() => expect(removeFleetNode).toHaveBeenCalledWith("spk_1"));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

test("recent logs load on demand", async () => {
  const fleetLogs = vi.fn().mockResolvedValue({entries: [{evidence_id: "e1", level: "error", source: "runtime", observed_at: "2026-09-27T10:00:00Z", message: "boom"}]});
  render(<SparkDetail api={{fleetNode: vi.fn().mockResolvedValue(node), fleetLogs} as unknown as ControlApi} id="spk_1" onClose={vi.fn()}/>);
  fireEvent.click(await screen.findByRole("button", {name: "Recent logs"}));
  expect(await screen.findByText(/boom/)).toBeVisible();
});

import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {KeysPage} from "./keys";
import type {ControlApi} from "../api/types";

const key = {name: "laptop", models: [], created_at: null, expires_at: null, last_used_at: null};

test("a created key is shown once and then dismissed", async () => {
  const gatewayKeys = vi.fn().mockResolvedValue({keys: []});
  const createGatewayKey = vi.fn().mockResolvedValue({...key, key: "sk-secret-value"});
  render(<KeysPage api={{gatewayKeys, createGatewayKey} as unknown as ControlApi}/>);
  fireEvent.click(await screen.findByRole("button", {name: "Create key"}));
  fireEvent.change(await screen.findByLabelText("Key name"), {target: {value: "laptop"}});
  fireEvent.click(screen.getByRole("button", {name: "Create"}));
  expect(await screen.findByLabelText("New key secret")).toHaveValue("sk-secret-value");
  expect(createGatewayKey).toHaveBeenCalledWith("laptop", [], "");
  fireEvent.click(screen.getByRole("button", {name: "I have copied it"}));
  expect(screen.queryByLabelText("New key secret")).toBeNull();
});

test("revoking a key needs its name typed first", async () => {
  // Break caught: revoke is irreversible, so a stray click must not do it.
  const revokeGatewayKey = vi.fn().mockResolvedValue({name: "laptop"});
  render(<KeysPage api={{gatewayKeys: vi.fn().mockResolvedValue({keys: [key]}), revokeGatewayKey} as unknown as ControlApi}/>);
  fireEvent.click(await screen.findByRole("button", {name: /Revoke/}));
  expect(screen.getByRole("button", {name: "Revoke key"})).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/Type/), {target: {value: "laptop"}});
  fireEvent.click(screen.getByRole("button", {name: "Revoke key"}));
  await waitFor(() => expect(revokeGatewayKey).toHaveBeenCalledWith("laptop"));
});

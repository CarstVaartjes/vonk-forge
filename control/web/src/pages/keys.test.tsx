import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { KeysPage } from "./keys";
import { ToastProvider } from "../components/toast";
import type { ControlApi } from "../api/types";

const key = { name: "laptop", models: [], created_at: null, expires_at: null, last_used_at: null };

test("a created key is shown once and then dismissed", async () => {
  const gatewayKeys = vi.fn().mockResolvedValue({ keys: [] });
  const createGatewayKey = vi.fn().mockResolvedValue({ ...key, key: "sk-secret-value" });
  render(<KeysPage api={{ gatewayKeys, createGatewayKey } as unknown as ControlApi} />);
  fireEvent.click(await screen.findByRole("button", { name: "Create key" }));
  fireEvent.change(await screen.findByLabelText("Key name"), { target: { value: "laptop" } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  expect(await screen.findByLabelText("New key secret")).toHaveValue("sk-secret-value");
  expect(createGatewayKey).toHaveBeenCalledWith("laptop", [], "");
  fireEvent.click(screen.getByRole("button", { name: "I have copied it" }));
  expect(screen.queryByLabelText("New key secret")).toBeNull();
});

test("revoking a key needs its name typed first", async () => {
  // Break caught: revoke is irreversible, so a stray click must not do it.
  const revokeGatewayKey = vi.fn().mockResolvedValue({ name: "laptop" });
  render(
    <ToastProvider>
      <KeysPage
        api={
          {
            gatewayKeys: vi.fn().mockResolvedValue({ keys: [key] }),
            revokeGatewayKey,
          } as unknown as ControlApi
        }
      />
    </ToastProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /Revoke/ }));
  expect(screen.getByRole("button", { name: "Revoke key" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "laptop" } });
  fireEvent.click(screen.getByRole("button", { name: "Revoke key" }));
  await waitFor(() => expect(revokeGatewayKey).toHaveBeenCalledWith("laptop"));
  // The outcome arrives as a toast in the live region, not as inline text.
  expect(
    await within(screen.getByRole("region", { name: "Notifications" })).findByText(/laptop/),
  ).toBeVisible();
});

test("rolling a key shows the new secret once", async () => {
  const rollGatewayKey = vi.fn().mockResolvedValue({ ...key, key: "sk-rolled-secret" });
  render(
    <ToastProvider>
      <KeysPage
        api={
          {
            gatewayKeys: vi.fn().mockResolvedValue({ keys: [key] }),
            rollGatewayKey,
          } as unknown as ControlApi
        }
      />
    </ToastProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /Roll/ }));
  fireEvent.click(screen.getByRole("button", { name: "Roll key" }));
  expect(await screen.findByLabelText("New key secret")).toHaveValue("sk-rolled-secret");
  expect(rollGatewayKey).toHaveBeenCalledWith("laptop");
});

test("an empty key list offers to create the first key", async () => {
  render(
    <ToastProvider>
      <KeysPage
        api={{ gatewayKeys: vi.fn().mockResolvedValue({ keys: [] }) } as unknown as ControlApi}
      />
    </ToastProvider>,
  );
  expect(await screen.findByRole("heading", { level: 2 })).toBeVisible();
  expect(screen.getAllByRole("button", { name: "Create key" }).length).toBeGreaterThan(1);
});

test("the create form explains the expiry format, hides while the secret shows, and failures render as a banner", async () => {
  const createGatewayKey = vi
    .fn()
    .mockRejectedValueOnce(new Error("Key name already exists"))
    .mockResolvedValueOnce({ ...key, key: "sk-secret-value" });
  render(
    <KeysPage
      api={
        {
          gatewayKeys: vi.fn().mockResolvedValue({ keys: [] }),
          createGatewayKey,
        } as unknown as ControlApi
      }
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "Create key" }));
  fireEvent.change(await screen.findByLabelText("Key name"), { target: { value: "laptop" } });
  expect(screen.getByLabelText("Expires after (optional)")).toHaveAccessibleDescription(
    "A number and a unit: s, m, h or d. For example 1h or 30d. Empty means never.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  const banner = await screen.findByRole("alert");
  expect(banner).toHaveTextContent("Key name already exists");
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  expect(await screen.findByLabelText("New key secret")).toHaveValue("sk-secret-value");
  expect(screen.queryByLabelText("Key name")).toBeNull();
  expect(screen.queryByRole("button", { name: "Create" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "I have copied it" }));
});

test("a create failure does not reappear after the panel is closed and reopened", async () => {
  const createGatewayKey = vi.fn().mockRejectedValue(new Error("Key name already exists"));
  render(
    <KeysPage
      api={
        {
          gatewayKeys: vi.fn().mockResolvedValue({ keys: [key] }),
          createGatewayKey,
        } as unknown as ControlApi
      }
    />,
  );
  const toggle = await screen.findByRole("button", { name: "Create key" });
  fireEvent.click(toggle);
  fireEvent.change(await screen.findByLabelText("Key name"), { target: { value: "laptop" } });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Key name already exists");
  fireEvent.click(toggle);
  fireEvent.click(toggle);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("an expired key is badged and a key without expiry reads Never", async () => {
  // Break caught: an expired key looked like a live one; "never" casing differed per column.
  const expired = { ...key, name: "old", expires_at: "2020-01-01T00:00:00Z" };
  render(
    <ToastProvider>
      <KeysPage
        api={
          {
            gatewayKeys: vi.fn().mockResolvedValue({ keys: [expired, key] }),
          } as unknown as ControlApi
        }
      />
    </ToastProvider>,
  );
  const rows = await screen.findAllByRole("row");
  const old = rows.find((row) => within(row).queryByRole("rowheader", { name: "old" }));
  expect(within(old as HTMLElement).getByText("Expired")).toBeVisible();
  expect(old).toHaveClass("is-expired");
  const live = rows.find((row) =>
    within(row).queryByRole("rowheader", { name: "laptop" }),
  ) as HTMLElement;
  expect(within(live).queryByText("Expired")).toBeNull();
  expect(within(live).getAllByText("Never")).toHaveLength(3);
});

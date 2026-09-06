import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {App} from "../app";
import type {ControlApi} from "../api/types";
import {AppShell} from "./app-shell";
import {Meter} from "./meter";
import {StatusPill} from "./status-pill";
import {FleetIcon} from "./icons";

const apiFixture = {
  audit: async () => ({events: []}),
  jobs: async () => ({jobs: [], next_cursor: null, total: 0}),
  visualFleet: async () => ({schema_version: 1, event_cursor: 0, generated_at: "2026-08-15T12:00:00Z", authority_revision: "a".repeat(64), nodes: []}),
  librarySnapshot: async () => ({schema_version: 1, generated_at: "2026-08-15T12:00:00Z", freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20}, models: [], unlinked_recipes: [], next_cursor: null}),
} as unknown as ControlApi;

afterEach(() => {
  history.replaceState(null, "", "/");
  localStorage.clear();
  sessionStorage.clear();
});

test("provides browser-equivalent local storage semantics", () => {
  // Break caught: a jsdom/Node boundary removes localStorage, so afterEach
  // aborts before Testing Library can remove the previously rendered page.
  localStorage.clear();
  expect(localStorage.length).toBe(0);
  expect(localStorage.getItem("missing")).toBeNull();

  localStorage.setItem(7 as unknown as string, false as unknown as string);
  localStorage.setItem("answer", "41");
  localStorage.setItem("answer", "42");

  expect(localStorage.length).toBe(2);
  expect(localStorage.key(0)).toBe("7");
  expect(localStorage.getItem("7")).toBe("false");
  expect(localStorage.getItem("answer")).toBe("42");

  localStorage.removeItem(7 as unknown as string);
  expect(localStorage.getItem("7")).toBeNull();
  expect(localStorage.length).toBe(1);

  localStorage.clear();
  expect(localStorage.length).toBe(0);
  expect(localStorage.key(0)).toBeNull();
});

test("keeps Fleet and Library as the two primary operating views", () => {
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined}>{null}</AppShell>);
  expect(screen.getByRole("link", {name: "Fleet"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Library"})).toBeVisible();
  expect(screen.queryByRole("link", {name: "Activity"})).not.toBeInTheDocument();
  expect(screen.queryByText("Agents")).not.toBeInTheDocument();
  expect(screen.queryByText("Catalog")).not.toBeInTheDocument();
  expect(screen.queryByText("Packages")).not.toBeInTheDocument();
  expect(screen.queryByText("Deployments")).not.toBeInTheDocument();
  expect(screen.queryByText("Updates")).not.toBeInTheDocument();
  expect(screen.queryByText("Jobs")).not.toBeInTheDocument();
});

test("keeps the focused workspace routes directly reachable in the compact header", () => {
  const {container} = render(<AppShell activeRoute="fleet" onNavigate={() => undefined}>{null}</AppShell>);

  expect(screen.getByRole("link", {name: "Fleet"})).toHaveAttribute("aria-current", "page");
  const library = screen.getByRole("link", {name: "Library"});
  expect(library).toHaveAttribute("href", "/library");
  expect(library).toBeVisible();

  const primary = screen.getByRole("navigation", {name: "Primary"});
  const routes = new Map([
    ["Fleet", "/fleet"],
    ["Library", "/library"],
  ]);
  for (const [name, href] of routes) {
    expect(within(primary).getByRole("link", {name})).toHaveAttribute("href", href);
  }
  for (const name of ["Agents", "Catalog", "Packages", "Deployments", "Updates", "Jobs", "Audit", "Profiles", "Models"]) {
    expect(within(primary).queryByRole("link", {name})).not.toBeInTheDocument();
  }
  for (const icon of container.querySelectorAll("svg")) {
    expect(icon).toHaveAttribute("aria-hidden", "true");
  }
});

test("renders a focused recovery page for unsupported URLs", async () => {
  render(<App api={apiFixture}/>);
  const user = userEvent.setup();

  await user.click(screen.getByRole("link", {name: "Library"}));

  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  expect(location.pathname).toBe("/library");
  expect(screen.getByRole("link", {name: "Library"})).toHaveAttribute("aria-current", "page");

  act(() => {
    history.pushState(null, "", "/unsupported-route");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  const notFound = await screen.findByRole("heading", {name: "Page not found"});
  await waitFor(() => expect(notFound).toHaveFocus());
  expect(screen.getByRole("link", {name: "Go to Fleet"})).toHaveAttribute("href", "/fleet");
  expect(screen.getByRole("link", {name: "Go to Library"})).toHaveAttribute("href", "/library");
  expect(document.title).toBe("Page not found · Vonk Forge");
  expect(screen.getByRole("link", {name: "Fleet"})).not.toHaveAttribute("aria-current");
  expect(screen.getByRole("link", {name: "Library"})).not.toHaveAttribute("aria-current");
  expect(location.pathname).toBe("/unsupported-route");

  await user.click(screen.getByRole("link", {name: "Go to Fleet"}));
  expect(await screen.findByRole("heading", {name: "Fleet"})).toBeVisible();
  expect(document.title).toBe("Fleet · Vonk Forge");
});

test("supports Activity as a secondary administrative route", async () => {
  history.replaceState(null, "", "/activity");
  render(<App api={apiFixture}/>);

  expect(await screen.findByRole("heading", {name: "Activity"})).toBeVisible();
  expect(screen.queryByRole("link", {name: "Activity"})).not.toBeInTheDocument();
  expect(location.pathname).toBe("/activity");
});

test("moves focus to main content after route activation", async () => {
  render(<App api={apiFixture}/>);
  const user = userEvent.setup();
  const library = screen.getByRole("link", {name: "Library"});

  library.focus();
  expect(library).toHaveFocus();
  await user.click(library);

  const heading = await screen.findByRole("heading", {name: "Library"});
  await waitFor(() => expect(heading).toHaveFocus());
});

test("keeps administrative actions behind the account menu", async () => {
  const user = userEvent.setup();
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined} operator={{
    subject: "admin@example.test",
    role: "Administrator",
    loggingOut: false,
    logoutError: "",
    onLogout: vi.fn(),
  }}>{null}</AppShell>);

  await user.click(screen.getByRole("button", {name: /admin@example.test/i}));
  const actions = screen.getByRole("group", {name: "Operator actions"});
  expect(within(actions).getByRole("link", {name: "Open Activity"})).toHaveAttribute("href", "/activity");
});

test("renders reusable status and capacity components with native semantics", () => {
  render(<><StatusPill tone="healthy">Ready</StatusPill><Meter label="Unified memory" value={24} max={32} valueLabel="24 of 32 GB"/></>);

  expect(screen.getByText("Ready")).toBeVisible();
  expect(screen.getByText("24 of 32 GB", {selector: "strong"})).toBeVisible();
  expect(screen.getByRole("meter", {name: "Unified memory"})).toHaveAttribute("value", "24");
  expect(screen.getByRole("meter", {name: "Unified memory"})).toHaveAttribute("max", "32");
});

test("decorative icons cannot be exposed by caller prop overrides", () => {
  const {container} = render(<FleetIcon aria-hidden={false} focusable="true"/>);
  const icon = container.querySelector("svg");

  expect(icon).toHaveAttribute("aria-hidden", "true");
  expect(icon).toHaveAttribute("focusable", "false");
});

test("makes shell routes visibly and keyboard disabled while navigation is locked", async () => {
  const onNavigate = vi.fn();
  const user = userEvent.setup();
  render(<AppShell activeRoute="library" navigationLocked onNavigate={onNavigate}>{null}</AppShell>);

  for (const name of ["Fleet", "Library"]) {
    const link = screen.getByRole("link", {name});
    expect(link).toHaveAttribute("aria-disabled", "true");
    expect(link).toHaveAttribute("tabindex", "-1");
  }

  await user.click(screen.getByRole("link", {name: "Fleet"}));
  expect(onNavigate).not.toHaveBeenCalled();
});

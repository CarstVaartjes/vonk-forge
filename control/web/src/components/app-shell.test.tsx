import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {App} from "../app";
import type {ControlApi} from "../api/types";
import {AppShell} from "./app-shell";
import {Meter} from "./meter";
import {StatusPill} from "./status-pill";
import {FleetIcon} from "./icons";

const apiFixture = {
  visualFleet: async () => ({schema_version: 1, event_cursor: 0, generated_at: "2026-08-15T12:00:00Z", repository_commit: "a".repeat(40), nodes: []}),
  updateSkew: async () => ({
    affected_nodes: [],
    digest: `sha256:${"b".repeat(64)}`,
    incompatible_nodes: [],
    nodes: [],
    offline_pending: [],
    prompt_required: false,
    target: {
      build_digest: `sha256:${"c".repeat(64)}`,
      platform_version: "1.0.0",
      protocol_maximum: 1,
      protocol_minimum: 1,
      release: `platform/releases/1.0.0/${"d".repeat(64)}.json`,
      release_digest: `sha256:${"d".repeat(64)}`,
      target_sha256: "d".repeat(64),
      tuf_targets_version: 1,
    },
  }),
  librarySnapshot: async () => ({schema_version: 1, generated_at: "2026-08-15T12:00:00Z", freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20}, models: [], unlinked_recipes: [], next_cursor: null}),
} as unknown as ControlApi;

afterEach(() => {
  history.replaceState(null, "", "/");
  localStorage.clear();
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

test("exposes only Fleet and Library as primary navigation", () => {
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined}>{null}</AppShell>);
  expect(screen.getByRole("link", {name: "Fleet"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Library"})).toBeVisible();
  expect(screen.queryByText("Agents")).not.toBeInTheDocument();
  expect(screen.queryByText("Catalog")).not.toBeInTheDocument();
  expect(screen.queryByText("Packages")).not.toBeInTheDocument();
  expect(screen.queryByText("Deployments")).not.toBeInTheDocument();
  expect(screen.queryByText("Updates")).not.toBeInTheDocument();
  expect(screen.queryByText("Jobs")).not.toBeInTheDocument();
});

test("keeps only Fleet and Library in primary navigation while preserving the mobile control", async () => {
  // Break caught: restoring any superseded primary route or removing the
  // accessible mobile control must fail this test.
  const {container} = render(<App api={apiFixture}/>);
  const user = userEvent.setup();

  expect(screen.getByRole("link", {name: "Fleet"})).toHaveAttribute("aria-current", "page");
  const library = screen.getByRole("link", {name: "Library"});
  expect(library).toHaveAttribute("href", "/library");
  expect(library).toBeVisible();

  const navigationToggle = screen.getByRole("button", {name: "Open system navigation"});
  expect(navigationToggle).toBeVisible();
  expect(navigationToggle).toHaveAttribute("aria-expanded", "false");
  await user.click(navigationToggle);
  expect(screen.getByRole("button", {name: "Close system navigation"})).toHaveAttribute("aria-expanded", "true");

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

test("opens the visual Library and falls legacy catalog URLs back to Fleet", async () => {
  // Break caught: Library disappears as a primary workspace or legacy catalog
  // URLs still render deleted page content.
  render(<App api={apiFixture}/>);
  const user = userEvent.setup();

  await user.click(screen.getByRole("link", {name: "Library"}));

  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  expect(location.pathname).toBe("/library");
  expect(screen.getByRole("link", {name: "Library"})).toHaveAttribute("aria-current", "page");

  history.pushState(null, "", "/catalog");
  dispatchEvent(new PopStateEvent("popstate"));
  expect(await screen.findByRole("heading", {name: "Fleet"})).toBeVisible();
  expect(location.pathname).toBe("/catalog");
});

test("moves focus to main content after mobile route activation", async () => {
  // Break caught: closing the mobile navigation leaves keyboard focus on the
  // activated link after its navigation container becomes hidden.
  render(<App api={apiFixture}/>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", {name: "Open system navigation"}));
  const library = screen.getByRole("link", {name: "Library"});

  library.focus();
  expect(library).toHaveFocus();
  await user.click(library);

  const heading = await screen.findByRole("heading", {name: "Library"});
  await waitFor(() => expect(heading).toHaveFocus());
  expect(screen.getByRole("button", {name: "Open system navigation"})).toHaveAttribute("aria-expanded", "false");
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

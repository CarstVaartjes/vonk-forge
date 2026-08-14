import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {App} from "../app";
import type {CatalogApi, ControlApi} from "../api/types";
import {Meter} from "./meter";
import {StatusPill} from "./status-pill";

const apiFixture = {
  fleet: async () => ({commit: "a".repeat(40), nodes: []}),
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
  catalogRecipes: async () => ({recipes: []}),
} as unknown as ControlApi & CatalogApi;

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

test("groups primary tasks without hiding administrative routes", async () => {
  // Break caught: restoring ten equal top-level links, stranding an existing
  // route, or removing the accessible mobile control must fail this test.
  const {container} = render(<App api={apiFixture}/>);
  const user = userEvent.setup();

  expect(screen.getByRole("link", {name: "Fleet"})).toHaveAttribute("aria-current", "page");
  const library = screen.getByRole("link", {name: "Library"});
  expect(library).toHaveAttribute("href", "/catalog");
  expect(library).toBeVisible();

  const navigationToggle = screen.getByRole("button", {name: "Open system navigation"});
  expect(navigationToggle).toBeVisible();
  expect(navigationToggle).toHaveAttribute("aria-expanded", "false");
  await user.click(navigationToggle);
  expect(screen.getByRole("button", {name: "Close system navigation"})).toHaveAttribute("aria-expanded", "true");

  await user.click(screen.getByText("Activity"));
  await user.click(screen.getByText("System"));

  const primary = screen.getByRole("navigation", {name: "Primary"});
  const routes = new Map([
    ["Fleet", "/fleet"],
    ["Library", "/catalog"],
    ["Deployments", "/deployments"],
    ["Updates", "/updates"],
    ["Jobs", "/jobs"],
    ["Audit", "/audit"],
    ["Agents", "/agents"],
    ["Profiles", "/profiles"],
    ["Models", "/models"],
    ["Packages", "/packages"],
  ]);
  for (const [name, href] of routes) {
    expect(within(primary).getByRole("link", {name})).toHaveAttribute("href", href);
  }
  for (const icon of container.querySelectorAll("svg")) {
    expect(icon).toHaveAttribute("aria-hidden", "true");
  }
});

test("opens Library on meaningful existing catalog content", async () => {
  // Break caught: Library falls back to Fleet, renders blank, or becomes a
  // label-only destination instead of resolving the current catalog slice.
  render(<App api={apiFixture}/>);
  const user = userEvent.setup();

  await user.click(screen.getByRole("link", {name: "Library"}));

  expect(await screen.findByRole("heading", {name: "Recipe catalog"})).toBeVisible();
  expect(location.pathname).toBe("/catalog");
  expect(screen.getByRole("link", {name: "Library"})).toHaveAttribute("aria-current", "page");
});

test("renders reusable status and capacity components with native semantics", () => {
  render(<><StatusPill tone="healthy">Ready</StatusPill><Meter label="Unified memory" value={24} max={32} valueLabel="24 of 32 GB"/></>);

  expect(screen.getByText("Ready")).toBeVisible();
  expect(screen.getByText("24 of 32 GB", {selector: "strong"})).toBeVisible();
  expect(screen.getByRole("meter", {name: "Unified memory"})).toHaveAttribute("value", "24");
  expect(screen.getByRole("meter", {name: "Unified memory"})).toHaveAttribute("max", "32");
});

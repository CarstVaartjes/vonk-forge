import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {App} from "../app";
import type {ControlApi, PublicRecipe} from "../api/types";
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

const importRecipe: PublicRecipe = {
  publisher: "vonk-forge", slug: "locked-import", title: "Locked import recipe", description: "A recipe used to verify locked navigation.", tags: ["test"],
  uri: `vonk://catalog/vonk-forge/locked-import@sha256:${"1".repeat(64)}`, content_sha256: "1".repeat(64),
  model_publisher: "models", model_slug: "locked", model_title: "Locked model", source_owner: "Vonk Forge", source_repository: "https://example.test/vonk-forge",
  capabilities: ["chat"], qualification: "candidate", qualification_basis: "explicit-candidate-metadata", qualification_detail: "Explicit candidate test evidence.", precision: "BF16",
  execution_harness: "vllm-openai", runtime_distribution: "vllm-test", source_bundle_sha256: "2".repeat(64), artifact_count: 1,
  topology_name: "single-spark", topology_mode: "single", node_count: 1, expected_download_bytes: 1, maximum_installed_bytes_per_node: 1, maximum_runtime_memory_bytes_per_node: 1,
  release_version: "1.0.0", release_released_at: "2026-08-24", local: {status: "not-imported", recipe_id: null, revision_number: null, content_sha256: null, release_version: null},
};

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

test("exposes Fleet, Library, and Activity as primary navigation", () => {
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined}>{null}</AppShell>);
  expect(screen.getByRole("link", {name: "Fleet"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Library"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Activity"})).toBeVisible();
  expect(screen.queryByText("Agents")).not.toBeInTheDocument();
  expect(screen.queryByText("Catalog")).not.toBeInTheDocument();
  expect(screen.queryByText("Packages")).not.toBeInTheDocument();
  expect(screen.queryByText("Deployments")).not.toBeInTheDocument();
  expect(screen.queryByText("Updates")).not.toBeInTheDocument();
  expect(screen.queryByText("Jobs")).not.toBeInTheDocument();
});

test("keeps the focused workspace routes in primary navigation while preserving the mobile control", async () => {
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
    ["Activity", "/activity"],
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

test("does not render a replacement page for unsupported URLs", async () => {
  // Break caught: an unsupported route silently falls back to Fleet or
  // Library, preserving compatibility behavior instead of disappearing.
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
  await waitFor(() => {
    expect(screen.queryByRole("heading", {name: "Fleet"})).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", {name: "Library"})).not.toBeInTheDocument();
  });
  expect(screen.getByRole("link", {name: "Fleet"})).not.toHaveAttribute("aria-current");
  expect(screen.getByRole("link", {name: "Library"})).not.toHaveAttribute("aria-current");
  expect(screen.getByRole("link", {name: "Activity"})).not.toHaveAttribute("aria-current");
  expect(location.pathname).toBe("/unsupported-route");
});

test("navigates to Activity as a first-class route", async () => {
  render(<App api={apiFixture}/>);
  const user = userEvent.setup();

  await user.click(screen.getByRole("link", {name: "Activity"}));

  expect(await screen.findByRole("heading", {name: "Activity"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Activity"})).toHaveAttribute("aria-current", "page");
  expect(location.pathname).toBe("/activity");
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

test("makes shell routes visibly and keyboard disabled while navigation is locked", async () => {
  const onNavigate = vi.fn();
  const user = userEvent.setup();
  render(<AppShell activeRoute="library" navigationLocked onNavigate={onNavigate}>{null}</AppShell>);

  for (const name of ["Fleet", "Library", "Activity"]) {
    const link = screen.getByRole("link", {name});
    expect(link).toHaveAttribute("aria-disabled", "true");
    expect(link).toHaveAttribute("tabindex", "-1");
  }

  await user.click(screen.getByRole("link", {name: "Fleet"}));
  expect(onNavigate).not.toHaveBeenCalled();
});

test("restores same-document navigation and warns before unload while an import is applying", async () => {
  history.replaceState(null, "", "/library/import");
  let resolveImport!: (value: {recipe_id: string; revision_number: number; lifecycle: string; slug: string}) => void;
  const importPending = new Promise<{recipe_id: string; revision_number: number; lifecycle: string; slug: string}>(resolve => { resolveImport = resolve; });
  const importPublicRecipe = vi.fn(() => importPending);
  const api = {
    ...apiFixture,
    listPublicRecipes: async () => ({repository: "vonk-forge-recipes", commit: "a".repeat(40), recipes: [importRecipe]}),
    previewPublicRecipe: async () => ({...importRecipe, source: "recipe_library" as const, changes_since_local: []}),
    importPublicRecipe,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  await user.click(await screen.findByRole("button", {name: "Review Locked import recipe"}));
  await user.click(await screen.findByRole("button", {name: "Continue to confirm"}));
  await user.click(await screen.findByRole("button", {name: "Import candidate"}));
  await waitFor(() => expect(importPublicRecipe).toHaveBeenCalledOnce());
  const lockedLocation = `${location.pathname}${location.search}`;
  expect(screen.getByRole("link", {name: "Fleet"})).toHaveAttribute("aria-disabled", "true");

  const unloadDuringImport = new Event("beforeunload", {cancelable: true});
  expect(dispatchEvent(unloadDuringImport)).toBe(false);
  expect(unloadDuringImport.defaultPrevented).toBe(true);

  act(() => {
    history.pushState(null, "", "/fleet");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await waitFor(() => expect(`${location.pathname}${location.search}`).toBe(lockedLocation));
  expect(screen.getByRole("heading", {name: "Locked import recipe"})).toBeVisible();

  resolveImport({recipe_id: "local-recipe", revision_number: 1, lifecycle: "draft", slug: "locked-import"});
  expect(await screen.findByText("Import complete")).toBeVisible();
  await waitFor(() => {
    const unloadAfterImport = new Event("beforeunload", {cancelable: true});
    expect(dispatchEvent(unloadAfterImport)).toBe(true);
    expect(unloadAfterImport.defaultPrevented).toBe(false);
  });
});

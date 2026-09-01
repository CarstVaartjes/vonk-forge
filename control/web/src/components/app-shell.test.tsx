import {act, fireEvent, render, screen, waitFor, within} from "@testing-library/react";
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
  model_publisher: "models", model_slug: "locked", model_title: "Locked model", model_version_publisher: "models", model_version_slug: "locked-bf16", model_version_title: "Locked model BF16", source_owner: "Vonk Forge", source_repository: "https://example.test/vonk-forge", alignment: "standard",
  capabilities: ["chat"], qualification: "candidate", qualification_basis: "explicit-candidate-metadata", qualification_detail: "Explicit candidate test evidence.", precision: "BF16", quantizations: ["BF16"],
  execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata", execution_readiness_detail: "Execution is declared for this shell navigation test.",
  execution_harness: "vllm-openai", runtime_distribution: "vllm-test", source_bundle_sha256: "2".repeat(64), artifact_count: 1,
  topology_name: "single-spark", topology_mode: "single", node_count: 1, expected_download_bytes: 1, maximum_installed_bytes_per_node: 1, maximum_runtime_memory_bytes_per_node: 1,
  topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}], fabric: {connectivity: "none", minimum_bandwidth_mbps: 0},
  release_version: "1.0.0", release_released_at: "2026-08-24", local: {status: "not-imported", recipe_id: null, revision_number: null, content_sha256: null, release_version: null},
};

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
  const dialog = screen.getByRole("dialog", {name: "Navigation"});
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(screen.getByRole("button", {name: "Close system navigation"})).toBeVisible();
  expect(document.querySelector("main")).toHaveAttribute("inert");
  expect(document.querySelector("main")).toHaveAttribute("aria-hidden", "true");
  expect(document.body).toHaveClass("shell-navigation-open");

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

test("closes the mobile navigation on its scrim and restores focus to the opener", async () => {
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined}><p>Workspace content</p></AppShell>);
  const user = userEvent.setup();
  const opener = screen.getByRole("button", {name: "Open system navigation"});

  await user.click(opener);
  expect(screen.getByRole("dialog", {name: "Navigation"})).toBeVisible();
  const scrim = document.querySelector<HTMLElement>(".shell-navigation-scrim");
  expect(scrim).not.toBeNull();
  fireEvent.pointerDown(scrim!);

  await waitFor(() => expect(screen.queryByRole("dialog", {name: "Navigation"})).not.toBeInTheDocument());
  expect(opener).toHaveFocus();
  expect(document.querySelector("main")).not.toHaveAttribute("inert");
  expect(document.querySelector("main")).not.toHaveAttribute("aria-hidden");
  expect(document.body).not.toHaveClass("shell-navigation-open");
});

test("keeps focus inside the mobile sheet and closes it on Escape", async () => {
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined}>{null}</AppShell>);
  const user = userEvent.setup();
  const opener = screen.getByRole("button", {name: "Open system navigation"});
  await user.click(opener);

  const close = screen.getByRole("button", {name: "Close system navigation"});
  close.focus();
  await user.keyboard("{Shift>}{Tab}{/Shift}");
  expect(screen.getByRole("link", {name: "Library"})).toHaveFocus();
  await user.keyboard("{Escape}");

  expect(screen.queryByRole("dialog", {name: "Navigation"})).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
});

test("closes the operator disclosure before closing its containing mobile sheet", async () => {
  render(<AppShell
    activeRoute="fleet"
    onNavigate={() => undefined}
    operator={{logoutError: "", loggingOut: false, onLogout: () => undefined, role: "Administrator", subject: "admin"}}
  >{null}</AppShell>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", {name: "Open system navigation"}));
  const operator = screen.getByRole("button", {name: /admin/i});
  await user.click(operator);
  expect(screen.getByRole("group", {name: "Operator actions"})).toBeVisible();

  await user.keyboard("{Escape}");

  expect(screen.queryByRole("group", {name: "Operator actions"})).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", {name: "Navigation"})).toBeVisible();
  expect(operator).toHaveFocus();
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

test("protects an edited custom recipe across shell navigation and clears it only after explicit discard", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={apiFixture}/>);

  const title = await screen.findByRole("textbox", {name: "Display name"});
  await user.clear(title);
  await user.type(title, "Protected draft");
  await waitFor(() => expect(sessionStorage.getItem("vonk-forge:custom-recipe-draft:v1")).not.toBeNull());

  const backToLibrary = screen.getByRole("button", {name: "Back to Library"});
  await user.click(backToLibrary);
  const backConfirmation = screen.getByRole("alertdialog", {name: "Discard this draft?"});
  expect(backConfirmation).toHaveTextContent("return to the Library");
  await user.click(within(backConfirmation).getByRole("button", {name: "Keep editing"}));
  expect(backToLibrary).toHaveFocus();
  expect(location.pathname).toBe("/library/create");

  const fleetLink = screen.getByRole("link", {name: "Fleet"});
  await user.click(fleetLink);
  const confirmation = screen.getByRole("alertdialog", {name: "Discard this draft?"});
  expect(location.pathname).toBe("/library/create");
  expect(screen.getByRole("textbox", {name: "Display name"})).toHaveValue("Protected draft");
  expect(within(confirmation).getByRole("button", {name: "Keep editing"})).toHaveFocus();

  await user.click(within(confirmation).getByRole("button", {name: "Keep editing"}));
  expect(fleetLink).toHaveFocus();
  expect(location.pathname).toBe("/library/create");

  const unload = new Event("beforeunload", {cancelable: true});
  expect(dispatchEvent(unload)).toBe(false);
  expect(unload.defaultPrevented).toBe(true);

  await user.click(fleetLink);
  await user.click(within(screen.getByRole("alertdialog", {name: "Discard this draft?"})).getByRole("button", {name: "Discard draft"}));
  expect(await screen.findByRole("heading", {name: "Fleet"})).toBeVisible();
  expect(location.pathname).toBe("/fleet");
  expect(sessionStorage.getItem("vonk-forge:custom-recipe-draft:v1")).toBeNull();
});

test("protects a custom recipe draft from browser history navigation", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={apiFixture}/>);

  const title = await screen.findByRole("textbox", {name: "Display name"});
  await user.clear(title);
  await user.type(title, "History-safe draft");

  act(() => {
    history.pushState(null, "", "/library");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  const firstConfirmation = await screen.findByRole("alertdialog", {name: "Discard this draft?"});
  expect(location.pathname).toBe("/library/create");
  await user.click(within(firstConfirmation).getByRole("button", {name: "Keep editing"}));
  expect(screen.getByRole("textbox", {name: "Display name"})).toHaveValue("History-safe draft");

  act(() => {
    history.pushState(null, "", "/library");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await user.click(within(await screen.findByRole("alertdialog", {name: "Discard this draft?"})).getByRole("button", {name: "Discard draft"}));
  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  expect(location.pathname).toBe("/library");
});

test("lets an untouched custom recipe leave without a discard prompt", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={apiFixture}/>);

  await screen.findByRole("heading", {name: "Create custom recipe"});
  await user.click(screen.getByRole("link", {name: "Fleet"}));

  expect(screen.queryByRole("alertdialog", {name: "Discard this draft?"})).not.toBeInTheDocument();
  expect(await screen.findByRole("heading", {name: "Fleet"})).toBeVisible();
});

test("focuses Library after leaving the builder within the same primary route", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={apiFixture}/>);

  await screen.findByRole("heading", {name: "Create custom recipe"});
  await user.click(screen.getByRole("button", {name: "Back to Library"}));

  const libraryHeading = await screen.findByRole("heading", {name: "Library"});
  await waitFor(() => expect(libraryHeading).toHaveFocus());
});

test("focuses Library after explicitly discarding an edited builder draft", async () => {
  history.replaceState(null, "", "/library/create");
  const user = userEvent.setup();
  render(<App api={apiFixture}/>);

  await user.type(await screen.findByRole("textbox", {name: "Display name"}), " edited");
  await user.click(screen.getByRole("button", {name: "Back to Library"}));
  await user.click(within(screen.getByRole("alertdialog", {name: "Discard this draft?"})).getByRole("button", {name: "Discard draft"}));

  const libraryHeading = await screen.findByRole("heading", {name: "Library"});
  await waitFor(() => expect(libraryHeading).toHaveFocus());
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

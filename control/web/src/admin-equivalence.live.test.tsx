import {readFileSync, writeFileSync} from "node:fs";
import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {ApiClient} from "./api/client";
import {App} from "./app";
import {AuthProvider} from "./auth";
import type {ReconciliationPlan} from "./api/types";

const origin = process.env.VONK_LIVE_ORIGIN;
const browserToken = process.env.VONK_LIVE_BROWSER_TOKEN;
const browserCsrf = process.env.VONK_LIVE_BROWSER_CSRF;
const stateFile = process.env.VONK_LIVE_STATE_FILE;
const expectedFile = process.env.VONK_LIVE_EXPECTED_FILE;
const resultFile = process.env.VONK_LIVE_RESULT_FILE;
const enabled = Boolean(origin && browserToken && browserCsrf && stateFile && expectedFile && resultFile);

(enabled ? it : it.skip)("crosses generated CLI and rendered browser clients against one live API", async () => {
  const expected = JSON.parse(readFileSync(expectedFile!, "utf8")) as ReconciliationPlan;
  const nativeFetch = globalThis.fetch;
  const applyBodies: unknown[] = [];
  let browserPlan: ReconciliationPlan | undefined;
  let raceNextApply = false;
  let staleStatus = 0;
  document.cookie = `vonk_session=${browserToken}; path=/`;
  document.cookie = `vonk_csrf=${browserCsrf}; path=/`;

  globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
    const request = input instanceof Request
      ? new Request(input, init)
      : new Request(new URL(String(input), origin), init);
    const headers = new Headers(request.headers);
    headers.set("Cookie", `vonk_session=${browserToken}; vonk_csrf=${browserCsrf}`);
    const authenticated = new Request(request, {headers});
    const url = new URL(authenticated.url);
    if (authenticated.method === "POST" && url.pathname === "/api/v1/reconciliations") {
      applyBodies.push(JSON.parse(await authenticated.clone().text()));
      if (raceNextApply) writeFileSync(stateFile!, '{"available":false}\n');
    }
    const response = await nativeFetch(authenticated);
    if (authenticated.method === "POST" && url.pathname.endsWith("/profiles/production-agents/plan") && response.ok) {
      browserPlan = await response.clone().json() as ReconciliationPlan;
    }
    if (raceNextApply && authenticated.method === "POST" && url.pathname === "/api/v1/reconciliations") {
      staleStatus = response.status;
      raceNextApply = false;
    }
    return response;
  };

  try {
    const api = new ApiClient();
    render(<AuthProvider api={api}><App api={api}/></AuthProvider>);
    const user = userEvent.setup();
    expect(await screen.findByText("admin")).toBeVisible();
    await user.click(screen.getByRole("link", {name: "Profiles"}));
    const profile = screen.getByLabelText("Profile ID to reconcile");
    await user.type(profile, "production-agents");
    await user.click(screen.getByRole("button", {name: "Preview exact plan"}));

    expect(await screen.findByText(expected.digest)).toBeVisible();
    expect(screen.getAllByText(expected.commit).length).toBeGreaterThan(0);
    for (const target of expected.targets) expect(screen.getAllByText(target).length).toBeGreaterThan(0);
    for (const operation of expected.operation_graph.nodes) {
      expect(screen.getByText(operation.operation_id)).toBeVisible();
      expect(screen.getByText(operation.kind)).toBeVisible();
    }

    const confirmation = screen.getByLabelText(/Type the exact plan digest/);
    await user.type(confirmation, expected.digest);
    await user.click(screen.getByRole("button", {name: "Apply exact plan"}));
    expect(await screen.findByText(/Plan accepted as job 11111111/)).toBeVisible();

    writeFileSync(stateFile!, '{"available":true}\n');
    await user.click(screen.getByRole("button", {name: "Preview exact plan"}));
    expect(await screen.findByText(expected.digest)).toBeVisible();
    raceNextApply = true;
    await user.type(screen.getByLabelText(/Type the exact plan digest/), expected.digest);
    await user.click(screen.getByRole("button", {name: "Apply exact plan"}));
    expect(await screen.findByRole("alert")).toHaveTextContent(/409/);
    expect(screen.getByRole("button", {name: "Apply exact plan"})).toBeDisabled();

    await user.click(screen.getByRole("button", {name: "Preview exact plan"}));
    const gate = await screen.findByRole("row", {name: new RegExp(expected.targets[0])});
    expect(within(gate).getByText(/unavailable/)).toBeVisible();
    await user.type(screen.getByLabelText(/Type the exact plan digest/), expected.digest);
    expect(screen.getByRole("button", {name: "Apply exact plan"})).toBeDisabled();
    expect(screen.queryByText("must-not-render.internal")).not.toBeInTheDocument();

    expect(browserPlan).toBeDefined();
    writeFileSync(resultFile!, JSON.stringify({
      apply_body: applyBodies[0],
      commit: browserPlan!.commit,
      digest: browserPlan!.digest,
      operations: browserPlan!.operation_graph.nodes,
      stale_status: staleStatus,
      targets: browserPlan!.targets,
      unavailable_visible: true,
    }));
  } finally {
    globalThis.fetch = nativeFetch;
  }
}, 20_000);

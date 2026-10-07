import {readFileSync} from "node:fs";
import {render, screen, cleanup} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi, test, expect} from "vitest";
import {ApiClient} from "../api/client";
import {ContractViolation} from "../api/contract-json";
import {CancelOperation} from "./cancel-operation";
import {ToastProvider} from "./toast";

// The focused hosted lane supplies responses emitted by the real PostgreSQL
// owner/API recovery journey, after generating consumers from the same source.
const responsesPath = process.env.VONK_CACHE_CANCEL_RESPONSE_OUTPUT;
test.skipIf(!responsesPath)("actual model cancellation responses preserve unknown and confirmed evidence in the browser", async () => {
  const responses = JSON.parse(readFileSync(responsesPath!, "utf8"));
  for (const name of ["unknown", "confirmed"]) {
    const response = responses[name];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(response), {status: 200, headers: {"content-type": "application/json"}})));
    const api = new ApiClient();
    const observed = await api.modelCacheOperation(response.operation_id);
    render(<ToastProvider><CancelOperation what="download" consequence="Keeps partial files." cancel={async () => observed} cancellationEvidence={result => result.cancellation?.observation?.detail ?? "Stopping the writer remains unconfirmed."}/></ToastProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", {name: "Cancel download"}));
    await user.click(screen.getByRole("button", {name: "Confirm cancel download"}));
    expect(await screen.findByText(observed.cancellation!.observation!.detail)).toBeVisible();
    expect(screen.queryByText("Cancelled the download.")).toBeNull();
    expect(observed.cancellation!.observation!.effect).toBe(name === "unknown" ? "unknown" : "stopped");
    cleanup();
    // A terminal state plus an issued stop is not confirmation. The browser
    // must enforce the owner's evidence constraint, not just its Python reader.
    const unconfirmedIssue = structuredClone(response);
    unconfirmedIssue.cancellation.observation.effect = "issued";
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(unconfirmedIssue), {status: 200, headers: {"content-type": "application/json"}})));
    await expect(api.modelCacheOperation(response.operation_id)).rejects.toBeInstanceOf(ContractViolation);
    vi.unstubAllGlobals();
  }
});

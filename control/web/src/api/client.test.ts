import {afterEach, expect, test, vi} from "vitest";
import {ApiClient} from "./client";
import {modelLibrary, recipeLibrary} from "../test-fixtures/library";

const SINCE = "2026-09-01T00:00:00.000Z";

function stubFetch(body: unknown): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    urls.push(request.url);
    return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
  });
  return urls;
}

function setCsrfCookie(): void {
  document.cookie = "vonk_csrf=test-csrf-token; Path=/";
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "vonk_csrf=; Max-Age=0; Path=/";
});

test("sends sort and updated_since to both library routes", async () => {
  // Break caught: the client accepts the server ordering arguments but drops
  // them from the query, so the page claims an order and a recency window the
  // API never applied.
  const modelUrls = stubFetch(modelLibrary);
  await new ApiClient().modelLibrary(undefined, "name", SINCE);
  const model = new URL(modelUrls[0]!);
  expect(model.pathname).toBe("/api/model/library");
  expect(model.searchParams.get("limit")).toBe("100");
  expect(model.searchParams.get("sort")).toBe("name");
  expect(model.searchParams.get("updated_since")).toBe(SINCE);

  const recipeUrls = stubFetch(recipeLibrary);
  await new ApiClient().recipeLibrary(undefined, "updated", SINCE);
  const recipe = new URL(recipeUrls[0]!);
  expect(recipe.pathname).toBe("/api/recipe/library");
  expect(recipe.searchParams.get("limit")).toBe("100");
  expect(recipe.searchParams.get("sort")).toBe("updated");
  expect(recipe.searchParams.get("updated_since")).toBe(SINCE);
});

test("creates a Fleet enrollment grant through the current operator endpoint", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    return new Response(JSON.stringify({action: "enroll", state: "created", grant: {}}), {
      status: 201,
      headers: {"Content-Type": "application/json"},
    });
  });
  setCsrfCookie();
  const input = {name: "Spark home", request_key: "00000000-0000-4000-8000-000000000101"};
  await new ApiClient().enrollFleetNode(input);
  expect(requests).toHaveLength(1);
  expect(requests[0]!.method).toBe("POST");
  expect(requests[0]!.headers.get("X-CSRF-Token")).toBe("test-csrf-token");
  expect(new URL(requests[0]!.url).pathname).toBe("/api/fleet/enroll");
  expect(await requests[0]!.json()).toEqual(input);
});

test("sends caller-owned request identities on artifact create, submit and cancel", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    return new Response(JSON.stringify({}), {status: 200, headers: {"Content-Type": "application/json"}});
  });
  setCsrfCookie();
  const client = new ApiClient();
  const createKey = "00000000-0000-4000-8000-000000000101";
  const submitKey = "00000000-0000-4000-8000-000000000102";
  const cancelKey = "00000000-0000-4000-8000-000000000103";

  await client.createArtifactJob("run/one", {} as never, createKey);
  await client.submitArtifactJob("job/one", submitKey);
  await client.cancelArtifactJob("job/one", "stop", cancelKey);
  await client.artifactJobByRequestId(createKey);

  expect(requests.map(request => [request.method, new URL(request.url).pathname, request.headers.get("X-Request-ID")])).toEqual([
    ["POST", "/api/recipe/runs/run%2Fone/artifact-jobs", createKey],
    ["POST", "/api/artifact-jobs/job%2Fone/submit", submitKey],
    ["POST", "/api/artifact-jobs/job%2Fone/cancel", cancelKey],
    ["GET", `/api/artifact-jobs/requests/${createKey}`, null],
  ]);
});

test("binds artifact result URLs to a canonical output name and digest", () => {
  const client = new ApiClient();
  const digest = "a".repeat(64);
  expect(client.artifactJobResultUrl("job/one", "frame.png", digest)).toBe(
    `/api/artifact-jobs/job%2Fone/results/frame.png/${digest}`,
  );
  expect(() => client.artifactJobResultUrl("job/one", "../frame.png", digest)).toThrow(
    "Unsafe artifact result name",
  );
  expect(() => client.artifactJobResultUrl("job/one", "frame.png", "A".repeat(64))).toThrow(
    "Unsafe artifact result digest",
  );
});

test("a failed call carries the Controller's request ID on the error", async () => {
  // Break caught: the X-Request-ID response header is dropped, so an operator
  // cannot quote the failed call to support.
  const id = "6f1c2f1e-0f3a-4c53-9a53-1b1a7d2f9a10";
  vi.stubGlobal("fetch", async () => new Response(JSON.stringify({detail: "no such Spark"}), {
    status: 404,
    headers: {"Content-Type": "application/json", "X-Request-ID": id},
  }));
  const failure = await new ApiClient().fleetNode("missing").then(() => null, error => error);
  expect(failure).toMatchObject({status: 404, requestId: id});
  expect(String(failure.message)).toContain(id);
});

test("refuses to send a mutating request when the CSRF token is missing", async () => {
  // Break caught: a missing vonk_csrf cookie silently dropped the CSRF header,
  // so the operator saw the Controller's generic 403 instead of the missing
  // token that caused it. Every mutation transport must refuse up front.
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    expect(request.method).toBe("GET");
    expect(new URL(request.url).pathname).toBe("/api/auth/session");
    return new Response(JSON.stringify({}), {status: 200});
  });
  const client = new ApiClient();
  const outcomes = await Promise.all([
    client.enrollFleetNode({name: "Spark home", request_key: "00000000-0000-4000-8000-000000000101"}),
    client.logout(),
    client.createArtifactJob("run/one", {} as never, "00000000-0000-4000-8000-000000000102"),
    client.uploadArtifactJobInput("job/one", {name: "input.bin", media_type: "application/octet-stream"} as never, new Blob(["payload"])),
  ].map(attempt => attempt.then(() => "sent", error => String(error.message))));
  for (const message of outcomes) expect(message).toContain("CSRF token missing");
});

test.each(["generated", "request", "logout", "token", "upload"])("expired cookies trigger authentication through %s without sending a mutation", async transport => {
  // Break caught: a missing CSRF cookie throws before the authentication
  // callback, leaving expired sessions stranded on pages without polling.
  const required = vi.fn();
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    expect(request.method).toBe("GET");
    expect(new URL(request.url).pathname).toBe("/api/auth/session");
    return new Response(null, {status: 401});
  });
  const client = new ApiClient();
  client.onAuthenticationRequired(required);
  const attempt = transport === "generated" ? client.enrollFleetNode({name: "Spark", request_key: crypto.randomUUID()})
    : transport === "request" ? client.createArtifactJob("run", {} as never, crypto.randomUUID())
    : transport === "logout" ? client.logout()
    : transport === "token" ? client.downloadCliToken()
    : client.uploadArtifactJobInput("job", {name: "input.bin", media_type: "application/octet-stream"} as never, new Blob(["payload"]));
  await expect(attempt).rejects.toBeInstanceOf(Error);
  expect(required).toHaveBeenCalledTimes(1);
});

test("refuses a request body without an explicit method", async () => {
  // Break caught: a body without a method silently became a GET and only
  // failed inside fetch with a generic TypeError, far from the caller's bug.
  vi.stubGlobal("fetch", async () => {
    throw new Error("the request must not be sent");
  });
  const failure = await new ApiClient().request("/api/model/library", {body: JSON.stringify({})}).then(() => null, error => error);
  expect(String(failure.message)).toContain("explicit method");
});

test("native artifact upload rejects an undeclared JSON receipt before exposing a DTO", async () => {
  let responseType = "";
  class Upload {
    status = 200;
    responseText = '{"unexpected":9007199254740993}';
    response = {unexpected: 9007199254740992};
    upload = {};
    onload?: () => void;
    open() {}
    setRequestHeader() {}
    getResponseHeader(name: string) { return name.toLowerCase() === "content-type" ? "application/json" : null; }
    set responseType(value: string) { responseType = value; }
    send() { this.onload?.(); }
  }
  vi.stubGlobal("XMLHttpRequest", Upload);
  setCsrfCookie();
  const request = new ApiClient().uploadArtifactJobInput("00000000-0000-4000-8000-000000000001", {
    slot: "prompt", name: "input.txt", media_type: "text/plain", size_bytes: 1, sha256: "a".repeat(64),
  }, new Blob(["x"]));
  await expect(request).rejects.toThrow("Invalid Control API contract");
  expect(responseType).toBe("text");
});

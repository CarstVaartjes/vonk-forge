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

afterEach(() => vi.unstubAllGlobals());

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

test("sends caller-owned request identities on artifact create, submit and cancel", async () => {
  const requests: Request[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    return new Response(JSON.stringify({}), {status: 200, headers: {"Content-Type": "application/json"}});
  });
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

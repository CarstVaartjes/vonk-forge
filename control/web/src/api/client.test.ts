import {
  EnrollmentGrantState,
  ErrorCategory,
  LifecycleState,
  WaitReason,
} from "./vocabulary.generated";
import { ApiError } from "./errors";
import { afterEach, expect, test, vi } from "vitest";
import { ApiClient } from "./client";
import { ContractResponseTooLarge, validateComponent } from "./contract-json";
import { isWireNumber, parseContractJson, stringifyContractJson } from "./contract-numeric";
import type {
  ArtifactJobCreateInput,
  ArtifactJobInputFile,
  ModelLibrary,
  RecipeLibrary,
} from "./types";
const libraryEnvelope = {
  generated_at: "2026-09-01T00:00:00Z",
  library: null,
  next_cursor: null,
  facets: {
    usage: [],
    family: [],
    version: [],
    quantization: [],
    publisher: [],
    creator: [],
    alignment: [],
    engine: [],
    sparks: [],
  },
  freshness_policy: {
    telemetry_live_seconds: 6,
    telemetry_delayed_seconds: 20,
    inventory_fresh_seconds: 300,
  },
};
const modelLibrary = { ...libraryEnvelope, models: [] } satisfies ModelLibrary;
const recipeLibrary = { ...libraryEnvelope, recipes: [] } satisfies RecipeLibrary;
const RUN_ID = "00000000-0000-4000-8000-000000000201";
const JOB_ID = "00000000-0000-4000-8000-000000000202";
const artifactCreate = {
  interface: "artifact-job",
  parameters: {},
  inputs: [],
  timeout_seconds: 60,
  output_limits: {
    max_files: 1,
    max_file_bytes: 1024,
    max_total_bytes: 1024,
    allowed_media_types: ["text/plain"],
  },
} satisfies ArtifactJobCreateInput;
const artifactInput = {
  slot: "prompt",
  name: "input.bin",
  media_type: "application/octet-stream",
  size_bytes: 7,
  sha256: "a".repeat(64),
} satisfies ArtifactJobInputFile;

const SINCE = "2026-09-01T00:00:00.000Z";

function stubFetch(body: unknown): string[] {
  const urls: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request =
      input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    urls.push(request.url);
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
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
    const request =
      input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    return new Response(
      JSON.stringify({
        action: "enroll",
        state: EnrollmentGrantState.PENDING,
        grant: {
          id: "00000000-0000-4000-8000-000000000101",
          expires_at: SINCE,
          purpose: "new-node",
          token: "a".repeat(43),
          controller_endpoint: "https://controller.invalid",
          enrollment_endpoint: "https://controller.invalid/api/enroll",
          ca_fingerprint: "a".repeat(64),
          installer_url: "https://install.vonkforge.ai/dev/spark",
          service_hostnames: [],
          controller_address: null,
        },
      }),
      {
        status: 201,
        headers: { "Content-Type": "application/json" },
      },
    );
  });
  setCsrfCookie();
  const input = { name: "Spark home", request_key: "00000000-0000-4000-8000-000000000101" };
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
    const request =
      input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    requests.push(request);
    return new Response(JSON.stringify({ detail: "artifact jobs are unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  });
  setCsrfCookie();
  const client = new ApiClient();
  const createKey = "00000000-0000-4000-8000-000000000101";
  const submitKey = "00000000-0000-4000-8000-000000000102";
  const cancelKey = "00000000-0000-4000-8000-000000000103";

  await expect(client.createArtifactJob(RUN_ID, artifactCreate, createKey)).rejects.toMatchObject({
    status: 503,
  });
  await expect(client.submitArtifactJob(JOB_ID, submitKey)).rejects.toMatchObject({ status: 503 });
  await expect(client.cancelArtifactJob(JOB_ID, "stop", cancelKey)).rejects.toMatchObject({
    status: 503,
  });
  await expect(client.artifactJobByRequestId(createKey)).rejects.toMatchObject({ status: 503 });

  expect(
    requests.map((request) => [
      request.method,
      new URL(request.url).pathname,
      request.headers.get("X-Request-ID"),
    ]),
  ).toEqual([
    ["POST", `/api/recipe/runs/${RUN_ID}/artifact-jobs`, createKey],
    ["POST", `/api/artifact-jobs/${JOB_ID}/submit`, submitKey],
    ["POST", `/api/artifact-jobs/${JOB_ID}/cancel`, cancelKey],
    ["GET", `/api/artifact-jobs/requests/${createKey}`, null],
  ]);
});

test.each(["1000.0", "-0.0"])(
  "artifact scalar %s retains its canonical finite float token through actual fetch",
  async (token) => {
    const value = parseContractJson(token);
    if (!isWireNumber(value)) throw new Error("canonical scalar fixture is not numeric");
    const input = {
      ...artifactCreate,
      parameters: { scale: value },
    } satisfies ArtifactJobCreateInput;
    validateComponent("ArtifactJobCreate", stringifyContractJson(input));
    const requests: Request[] = [];
    vi.stubGlobal("fetch", async (raw: RequestInfo | URL, init?: RequestInit) => {
      const request =
        raw instanceof Request ? raw : new Request(new URL(String(raw), location.origin), init);
      requests.push(request);
      return new Response('{"detail":"artifact jobs are unavailable"}', {
        status: 503,
        headers: { "content-type": "application/json" },
      });
    });
    setCsrfCookie();
    await expect(
      new ApiClient().createArtifactJob(RUN_ID, input, "00000000-0000-4000-8000-000000000101"),
    ).rejects.toMatchObject({ status: 503 });
    expect(requests).toHaveLength(1);
    const body = await requests[0]!.text();
    validateComponent("ArtifactJobCreate", body);
    expect(body).toContain(`"scale":${token}`);
  },
);

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
  vi.stubGlobal(
    "fetch",
    async () =>
      new Response(JSON.stringify({ detail: "no such Spark" }), {
        status: 404,
        headers: { "Content-Type": "application/json", "X-Request-ID": id },
      }),
  );
  const failure = await new ApiClient().fleetNode("missing").then(
    () => null,
    (error) => error,
  );
  expect(failure).toMatchObject({ status: 404, requestId: id });
  expect(String(failure.message)).toContain(id);
});

test("refuses to send a mutating request when the CSRF token is missing", async () => {
  document.cookie = "vonk_csrf=; Max-Age=0; Path=/";
  // Break caught: a missing vonk_csrf cookie silently dropped the CSRF header,
  // so the operator saw the Controller's generic 403 instead of the missing
  // token that caused it. Every mutation transport must refuse up front.
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request =
      input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    expect(request.method).toBe("GET");
    expect(new URL(request.url).pathname).toBe("/api/auth/session");
    return new Response(
      JSON.stringify({ subject: "admin", role: "administrator", expires_at: SINCE }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  });
  const client = new ApiClient();
  const outcomes = await Promise.all(
    [
      client.enrollFleetNode({
        name: "Spark home",
        request_key: "00000000-0000-4000-8000-000000000101",
      }),
      client.logout(),
      client.createArtifactJob(RUN_ID, artifactCreate, "00000000-0000-4000-8000-000000000102"),
      client.uploadArtifactJobInput(JOB_ID, artifactInput, new Blob(["payload"])),
    ].map((attempt) =>
      attempt.then(
        () => "sent",
        (error) => String(error.message),
      ),
    ),
  );
  for (const message of outcomes) expect(message).toContain("CSRF token missing");
});

test.each(["generated", "request", "logout", "token", "upload"])(
  "expired cookies trigger authentication through %s without sending a mutation",
  async (transport) => {
    document.cookie = "vonk_csrf=; Max-Age=0; Path=/";
    // Break caught: a missing CSRF cookie throws before the authentication
    // callback, leaving expired sessions stranded on pages without polling.
    const required = vi.fn();
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), location.origin), init);
      expect(request.method).toBe("GET");
      expect(new URL(request.url).pathname).toBe("/api/auth/session");
      return new Response(JSON.stringify({ detail: "authentication required" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    });
    const client = new ApiClient();
    client.onAuthenticationRequired(required);
    const attempt =
      transport === "generated"
        ? client.enrollFleetNode({ name: "Spark", request_key: crypto.randomUUID() })
        : transport === "request"
          ? client.createArtifactJob(RUN_ID, artifactCreate, crypto.randomUUID())
          : transport === "logout"
            ? client.logout()
            : transport === "token"
              ? client.downloadCliToken()
              : client.uploadArtifactJobInput(JOB_ID, artifactInput, new Blob(["payload"]));
    await expect(attempt).rejects.toBeInstanceOf(Error);
    expect(required).toHaveBeenCalledTimes(1);
  },
);

test("refuses a request body without an explicit method", async () => {
  // Break caught: a body without a method silently became a GET and only
  // failed inside fetch with a generic TypeError, far from the caller's bug.
  vi.stubGlobal("fetch", async () => {
    throw new Error("the request must not be sent");
  });
  const failure = await new ApiClient()
    .request("/api/model/library", { body: JSON.stringify({}) })
    .then(
      () => null,
      (error) => error,
    );
  expect(String(failure.message)).toContain("explicit method");
});

test("native artifact upload rejects an undeclared JSON receipt before exposing a DTO", async () => {
  let responseType = "";
  class Upload {
    status = 200;
    responseText = '{"unexpected":9007199254740993}';
    response = { unexpected: 9007199254740992 };
    upload = {};
    onload?: () => void;
    open() {}
    setRequestHeader() {}
    getResponseHeader(name: string) {
      return name.toLowerCase() === "content-type" ? "application/json" : null;
    }
    set responseType(value: string) {
      responseType = value;
    }
    send() {
      this.onload?.();
    }
  }
  vi.stubGlobal("XMLHttpRequest", Upload);
  setCsrfCookie();
  const request = new ApiClient().uploadArtifactJobInput(
    "00000000-0000-4000-8000-000000000001",
    {
      slot: "prompt",
      name: "input.txt",
      media_type: "text/plain",
      size_bytes: 1,
      sha256: "a".repeat(64),
    },
    new Blob(["x"]),
  );
  await expect(request).rejects.toThrow("Invalid Control API contract");
  expect(responseType).toBe("text");
});

test.each(["bespoke", "generated"])(
  "%s JSON consumer uses the same producer-owned streaming budget",
  async (path) => {
    const cancel = vi.fn();
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(
          new ReadableStream<Uint8Array>(
            {
              pull(controller) {
                controller.enqueue(new Uint8Array(1048577));
              },
              cancel,
            },
            { highWaterMark: 0 },
          ),
          { headers: { "content-type": "application/json" } },
        ),
    );
    const client = new ApiClient();
    const request = path === "bespoke" ? client.request("/api/operations") : client.operations();
    await expect(request).rejects.toBeInstanceOf(ContractResponseTooLarge);
    expect(cancel).toHaveBeenCalledOnce();
  },
);

test("gateway uncertainty preserves its reason and a fresh observation succeeds", async () => {
  const { ErrorCategory, WaitReason } = await import("./vocabulary.generated");
  let unavailable = true;
  vi.stubGlobal(
    "fetch",
    async () =>
      new Response(
        JSON.stringify(
          unavailable
            ? { category: ErrorCategory.UNKNOWN, reason: WaitReason.OBSERVATION_UNAVAILABLE }
            : { keys: [] },
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
  );
  const client = new ApiClient();
  await expect(client.gatewayKeys()).rejects.toMatchObject({
    status: 200,
    message: WaitReason.OBSERVATION_UNAVAILABLE,
  });
  unavailable = false;
  await expect(client.gatewayKeys()).resolves.toEqual({ keys: [] });
});

test.each(["create", "roll"])(
  "gateway %s reconnects to the exact request after a lost response",
  async (action) => {
    const receipts = new Map<string, string>();
    let effects = 0;
    let loseReply = true;
    setCsrfCookie();
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request =
        input instanceof Request
          ? input
          : new Request(new URL(String(input), location.origin), init);
      const identity =
        action === "create"
          ? (await request.json()).request_id
          : new URL(request.url).searchParams.get("request_id");
      if (!receipts.has(identity)) receipts.set(identity, `sk-generation-${++effects}`);
      if (loseReply) {
        loseReply = false;
        throw new Error("response lost after effect");
      }
      return new Response(
        JSON.stringify({ name: "client", models: [], key: receipts.get(identity) }),
        {
          status: action === "create" ? 201 : 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    });
    const client = new ApiClient();
    const invoke = (identity: string) =>
      action === "create"
        ? client.createGatewayKey("client", [], undefined, identity)
        : client.rollGatewayKey("client", identity);
    await invoke("first").catch(() => undefined);
    const first = await invoke("first");
    expect(first.key).toBe(receipts.get("first"));
    expect(effects).toBe(1);
    const second = await invoke("second");
    expect(second.key).not.toBe(first.key);
    expect(effects).toBe(2);
  },
);
test("an unknown enrollment revocation preserves observation and admits a fresh revocation", async () => {
  setCsrfCookie();
  const grantId = "00000000-0000-4000-8000-000000000101";
  const replies = [
    {
      category: ErrorCategory.UNKNOWN,
      reason: WaitReason.OBSERVATION_UNAVAILABLE,
      state: LifecycleState.FAILED,
    },
    {
      id: grantId,
      state: EnrollmentGrantState.REVOKED,
      purpose: null,
      node_id: null,
      display_name: null,
      consumed_at: null,
      revoked_at: SINCE,
      expires_at: SINCE,
    },
  ];
  vi.stubGlobal(
    "fetch",
    async () =>
      new Response(JSON.stringify(replies.shift()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  const client = new ApiClient();
  await expect(client.revokeEnrollment(grantId)).rejects.toBeInstanceOf(ApiError);
  expect((await client.revokeEnrollment(grantId)).state).toBe(EnrollmentGrantState.REVOKED);
});

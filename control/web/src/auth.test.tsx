import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {App} from "./app";
import {AuthProvider, AuthenticationRequired} from "./auth";
import {ApiClient} from "./api/client";
import type {ControlApi} from "./api/types";

const session = {
  subject: "admin",
  role: "administrator" as const,
  expires_at: "2026-08-13T21:30:00Z",
};

type BrowserAuthApi = {
  session(): Promise<typeof session>;
  login(subject: "admin", password: string): Promise<typeof session>;
  logout(): Promise<void>;
  onAuthenticationRequired(listener: () => void): () => void;
};

type TestApi = ControlApi & BrowserAuthApi;

function controlApi(overrides: Partial<TestApi> = {}): TestApi {
  return {
    visualFleet: async () => ({schema_version: 1, event_cursor: 0, generated_at: "2026-08-15T12:00:00Z", repository_commit: "a".repeat(40), nodes: []}),
    updateSkew: async () => ({affected_nodes: [], digest: "sha256:" + "b".repeat(64), incompatible_nodes: [], nodes: [], offline_pending: [], prompt_required: false, target: {build_digest: "sha256:" + "c".repeat(64), platform_version: "1.0.0", protocol_maximum: 1, protocol_minimum: 1, release: "platform/releases/1.0.0/" + "d".repeat(64) + ".json", release_digest: "sha256:" + "d".repeat(64), target_sha256: "d".repeat(64), tuf_targets_version: 1}}),
    session: async () => { throw new AuthenticationRequired(); },
    login: async () => session,
    logout: async () => undefined,
    onAuthenticationRequired: () => () => undefined,
    ...overrides,
  } as TestApi;
}

async function openOperatorMenu(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(screen.getByRole("button", {name: /admin/i}));
  expect(await screen.findByRole("dialog", {name: "Operator menu"})).toBeVisible();
}

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

it("keeps the control shell hidden until an unauthenticated startup check reaches the login page", async () => {
  // Break caught: Fleet or navigation render before the browser has verified a
  // durable cookie session, exposing the operator surface to anonymous users.
  let resolveSession!: (value: typeof session) => void;
  const pendingSession = new Promise<typeof session>(resolve => { resolveSession = resolve; });
  const api = controlApi({session: async () => pendingSession});

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);

  expect(screen.getByRole("status")).toHaveTextContent("Checking administrator session");
  expect(screen.queryByRole("navigation", {name: "Primary"})).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", {name: "Fleet"})).not.toBeInTheDocument();

  resolveSession(session);
  expect(await screen.findByRole("heading", {name: "Fleet"})).toBeVisible();
});

it("shows only the administrator sign-in form after a 401 session check", async () => {
  // Break caught: an expired or absent durable cookie leaves a partially
  // rendered administrator shell, or the login form loses password-manager semantics.
  const api = controlApi();

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);

  expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
  expect(screen.queryByRole("navigation", {name: "Primary"})).not.toBeInTheDocument();
  expect(screen.getByLabelText("Administrator account")).toHaveValue("admin");
  expect(screen.getByLabelText("Administrator account")).toHaveAttribute("autocomplete", "username");
  expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "current-password");
});

it("renders the bounded throttle guidance for an ApiClient login 429", async () => {
  // Break caught: ApiClient loses the HTTP status from a real throttle
  // response, so the login page shows generic guidance instead of the
  // bounded retry message.
  const paths: string[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.origin), init);
    const path = new URL(request.url).pathname;
    paths.push(path);
    if (path === "/api/v1/auth/session") {
      return new Response(JSON.stringify({detail: "authentication failed"}), {status: 401});
    }
    return new Response(JSON.stringify({detail: "authentication temporarily unavailable"}), {status: 429});
  });
  const api = new ApiClient();
  const user = userEvent.setup();

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);
  await screen.findByRole("heading", {name: "Sign in"});
  await user.type(screen.getByLabelText("Password"), "synthetic-test-password");
  await user.click(screen.getByRole("button", {name: "Sign in"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("Sign in is temporarily unavailable. Please try again.");
  expect(paths).toEqual(["/api/v1/auth/session", "/api/v1/auth/login"]);
});

it("opens Fleet with the authenticated administrator identity after a successful login", async () => {
  // Break caught: login accepts credentials but fails to establish an
  // authenticated shell or omits the authority/environment identity cues.
  const login = vi.fn(async () => session);
  const api = controlApi({login});
  const user = userEvent.setup();

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);
  await screen.findByRole("heading", {name: "Sign in"});
  await user.type(screen.getByLabelText("Password"), "synthetic-test-password");
  await user.click(screen.getByRole("button", {name: "Sign in"}));

  expect(await screen.findByRole("heading", {name: "Fleet"})).toBeVisible();
  expect(screen.getByText("admin")).toBeVisible();
  expect(screen.getByText("Administrator")).toBeVisible();
  expect(screen.getByText("Development")).toBeVisible();
  await waitFor(() => expect(login).toHaveBeenCalledWith("admin", "synthetic-test-password"));
});

it("returns the entire shell to login once when the API reports an expired session", async () => {
  // Break caught: a 401 from a normal API request leaves stale privileged
  // content in view or triggers repeated session/reload work.
  let expire!: () => void;
  const api = controlApi({
    session: async () => session,
    onAuthenticationRequired: listener => {
      expire = listener;
      return () => undefined;
    },
  });

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);
  await screen.findByRole("heading", {name: "Fleet"});
  expire();

  expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
  expect(screen.queryByRole("navigation", {name: "Primary"})).not.toBeInTheDocument();
});

it("waits for server logout before removing the authenticated shell", async () => {
  // Break caught: clicking Logout only changes local state before the server
  // has acknowledged revocation, making a failed logout look successful.
  let resolveLogout!: () => void;
  const pendingLogout = new Promise<void>(resolve => { resolveLogout = resolve; });
  const api = controlApi({session: async () => session, logout: async () => pendingLogout});
  const user = userEvent.setup();

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);
  await screen.findByRole("heading", {name: "Fleet"});
  await openOperatorMenu(user);
  await user.click(screen.getByRole("button", {name: "Logout"}));

  expect(screen.getByRole("heading", {name: "Fleet"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Signing out…"})).toBeDisabled();
  resolveLogout();
  expect(await screen.findByRole("heading", {name: "Sign in"})).toBeVisible();
});

it("keeps the authenticated shell visible when logout fails", async () => {
  // Break caught: an unavailable logout endpoint clears the local shell even
  // though the browser's durable session may still be server-valid.
  const api = controlApi({session: async () => session, logout: async () => { throw new Error("synthetic logout failure"); }});
  const user = userEvent.setup();

  render(<AuthProvider api={api}><App api={api}/></AuthProvider>);
  await screen.findByRole("heading", {name: "Fleet"});
  await openOperatorMenu(user);
  await user.click(screen.getByRole("button", {name: "Logout"}));

  expect(await screen.findByRole("alert")).toHaveTextContent("Unable to sign out. Your session may still be active.");
  expect(screen.getByRole("heading", {name: "Fleet"})).toBeVisible();
});

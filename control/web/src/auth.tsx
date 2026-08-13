import {createContext, useContext, useEffect, useState} from "react";
import type {ReactNode} from "react";
import type {AuthSession} from "./api/types";
import {LoginPage} from "./pages/login";

export type {AuthSession} from "./api/types";

export class AuthenticationRequired extends Error {
  readonly status = 401;

  constructor() {
    super("Authentication is required");
    this.name = "AuthenticationRequired";
  }
}

export type BrowserAuthApi = {
  session(): Promise<AuthSession>;
  login(subject: "admin", password: string): Promise<AuthSession>;
  logout(): Promise<void>;
  onAuthenticationRequired(listener: () => void): () => void;
};

type AuthContextValue = {
  session: AuthSession;
  logout(): Promise<void>;
  loggingOut: boolean;
  logoutError: string;
};

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

export function useOptionalAuth(): AuthContextValue | undefined {
  return useContext(AuthContext);
}

export function AuthProvider({api, children}: {api: BrowserAuthApi; children: ReactNode}) {
  const [session, setSession] = useState<AuthSession>();
  const [checking, setChecking] = useState(true);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  useEffect(() => {
    let active = true;
    const unsubscribe = api.onAuthenticationRequired(() => {
      if (active) setSession(undefined);
    });
    api.session().then(value => {
      if (active) setSession(value);
    }).catch(() => {
      if (active) setSession(undefined);
    }).finally(() => {
      if (active) setChecking(false);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [api]);

  async function login(subject: "admin", password: string): Promise<void> {
    const authenticated = await api.login(subject, password);
    setLogoutError("");
    setSession(authenticated);
  }

  async function logout(): Promise<void> {
    setLogoutError("");
    setLoggingOut(true);
    try {
      await api.logout();
      setSession(undefined);
    } catch {
      setLogoutError("Unable to sign out. Your session may still be active.");
    } finally {
      setLoggingOut(false);
    }
  }

  if (checking) return <main className="auth-loading"><p role="status">Checking administrator session…</p></main>;
  if (!session) return <LoginPage onLogin={login}/>;
  return <AuthContext.Provider value={{session, logout, loggingOut, logoutError}}>{children}</AuthContext.Provider>;
}

import {useEffect, useRef, useState} from "react";

type LoginFailure = {status?: unknown};

function loginMessage(value: unknown): string {
  const status = typeof value === "object" && value !== null
    ? (value as LoginFailure).status
    : undefined;
  if (status === 401) return "Sign in failed. Check your credentials and try again.";
  if (status === 429) return "Sign in is temporarily unavailable. Please try again.";
  return "Unable to sign in. Please try again.";
}

export function LoginPage({onLogin}: {onLogin(subject: "admin", password: string): Promise<void>}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const passwordField = useRef<HTMLInputElement>(null);
  useEffect(() => { passwordField.current?.focus(); }, []);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!password || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      await onLogin("admin", password);
    } catch (value) {
      setError(loginMessage(value));
      queueMicrotask(() => passwordField.current?.focus());
    } finally {
      setPassword("");
      setSubmitting(false);
    }
  }

  return <main className="login-shell">
    <section className="login-card" aria-labelledby="login-heading">
      <span className="mark" aria-hidden="true">VF</span>
      <h1 id="login-heading">Sign in</h1>
      <p>Use the local administrator account to manage this Vonk Forge cluster.</p>
      <form onSubmit={event => void submit(event)}>
        <label>Administrator account
          <input name="username" value="admin" readOnly autoComplete="username"/>
        </label>
        <label>Password
          <input ref={passwordField} name="password" type="password" value={password} autoComplete="current-password" onChange={event => setPassword(event.target.value)} required/>
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={!password || submitting}>{submitting ? "Signing in…" : "Sign in"}</button>
      </form>
    </section>
  </main>;
}

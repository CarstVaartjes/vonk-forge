import {useCallback, useEffect, useState, type FormEvent} from "react";
import type {ControlApi, GatewayKeyCreated, GatewayKeyList} from "../api/types";
import {ConfirmPanel} from "../components/confirm-panel";
import {CopyButton} from "../components/copy-button";
import {Time} from "../components/time";
import {safeErrorText} from "../lib/error-display";

function failure(error: unknown, fallback: string): string {
  return safeErrorText(error instanceof Error ? error.message : fallback);
}

export function KeysPage({api}: {api: ControlApi}) {
  const [keys, setKeys] = useState<GatewayKeyList["keys"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [models, setModels] = useState("");
  const [expires, setExpires] = useState("");
  const [created, setCreated] = useState<GatewayKeyCreated | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try { setKeys((await api.gatewayKeys(signal)).keys); setError(null); }
    catch (cause) { if (!signal?.aborted) setError(failure(cause, "Could not read client keys")); }
  }, [api]);
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [load]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true); setError(null);
    try {
      setCreated(await api.createGatewayKey(name.trim(), models.split(/[\s,]+/).filter(Boolean), expires.trim()));
      setName(""); setModels(""); setExpires("");
      await load();
    } catch (cause) { setError(failure(cause, "Could not create the key")); } finally { setBusy(false); }
  }

  async function revoke(keyName: string) {
    setBusy(true); setError(null);
    try { await api.revokeGatewayKey(keyName); setRevoking(null); await load(); }
    catch (cause) { setError(failure(cause, "Could not revoke the key")); } finally { setBusy(false); }
  }

  return <div className="fleet-page keys-page">
    <header className="fleet-command-header"><div className="fleet-command-title"><h1>Keys</h1><p>Client keys let apps call the models the fleet serves.</p></div></header>
    <section className="fleet-enrollment" aria-labelledby="key-create-heading">
      <div><h2 id="key-create-heading">Create a key</h2><p>Leave models empty to allow every model. CLI: <code>vonkctl key create &lt;name&gt;</code></p></div>
      <form onSubmit={event => void create(event)}>
        <label htmlFor="key-name">Key name</label><input id="key-name" required maxLength={256} value={name} onChange={event => setName(event.currentTarget.value)} placeholder="e.g. laptop"/>
        <label htmlFor="key-models">Models (optional, separated by spaces)</label><input id="key-models" value={models} onChange={event => setModels(event.currentTarget.value)}/>
        <label htmlFor="key-expires">Expires after (optional, e.g. 30d or 12h)</label><input id="key-expires" pattern="[1-9][0-9]{0,5}[smhd]" value={expires} onChange={event => setExpires(event.currentTarget.value)}/>
        <button type="submit" className="button" disabled={busy}>Create key</button>
      </form>
      {created && <div className="fleet-enrollment-result" role="status">
        <p><strong>Key {created.name} created.</strong> Copy it now. You won't see this key again.</p>
        <input readOnly aria-label="New key secret" value={created.key}/><CopyButton label="key" value={created.key}/>
        <button type="button" className="button secondary" onClick={() => setCreated(null)}>I have copied it</button>
      </div>}
    </section>
    {error && <p role="alert">{error} <button type="button" className="button secondary" onClick={() => void load()}>Retry</button></p>}
    {keys?.length === 0 && <section className="fleet-empty"><h2>No client keys</h2><p>Create a key above so an app can call the fleet's models.</p></section>}
    {keys && keys.length > 0 && <section className="fleet-compact" aria-label="Client keys"><div className="fleet-table-scroll"><table>
      <caption className="sr-only">Client keys, never secrets</caption>
      <thead><tr><th scope="col">Name</th><th scope="col">Models</th><th scope="col">Created</th><th scope="col">Last used</th><th scope="col">Expires</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
      <tbody>{keys.map(key => <tr key={key.name}>
        <th scope="row">{key.name}</th><td>{key.models.length ? key.models.join(", ") : "All models"}</td>
        <td><Time value={key.created_at}/></td><td><Time value={key.last_used_at}/></td><td>{key.expires_at ? <Time value={key.expires_at}/> : "Never"}</td>
        <td><button type="button" className="button secondary" onClick={() => setRevoking(key.name)}>Revoke<span className="sr-only"> {key.name}</span></button></td>
      </tr>)}</tbody></table></div></section>}
    {revoking && <ConfirmPanel title={`Revoke key ${revoking}?`} consequence="Apps using this key stop working immediately. This cannot be undone." confirmLabel="Revoke key" command={`vonkctl key revoke ${revoking}`} busy={busy} onConfirm={() => void revoke(revoking)} onCancel={() => setRevoking(null)}/>}
  </div>;
}

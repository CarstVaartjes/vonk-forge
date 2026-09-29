import {useCallback, useEffect, useState, type FormEvent} from "react";
import type {ControlApi, GatewayKeyCreated, GatewayKeyList} from "../api/types";
import {ConfirmDialog} from "../components/confirm-dialog";
import {CopyButton} from "../components/copy-button";
import {EmptyState} from "../components/empty-state";
import {PageHeader} from "../components/page-header";
import {SkeletonRows} from "../components/skeleton";
import {useToast} from "../components/toast";
import {SortHeader, useSort} from "../components/sort-header";
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
  const [rolling, setRolling] = useState<string | null>(null);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try { setKeys((await api.gatewayKeys(signal)).keys); setError(null); }
    catch (cause) { if (!signal?.aborted) setError(failure(cause, "Could not read API keys")); }
  }, [api]);
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [load]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const key = await api.createGatewayKey(name.trim(), models.split(/[\s,]+/).filter(Boolean), expires.trim());
      setCreated(key);
      toast.success(`Key ${key.name} created. Copy the secret now.`);
      setName(""); setModels(""); setExpires("");
      await load();
    } catch (cause) { toast.error(failure(cause, "Could not create the key")); } finally { setBusy(false); }
  }

  async function revoke(keyName: string) {
    setBusy(true);
    try { await api.revokeGatewayKey(keyName); toast.success(`Key ${keyName} revoked.`); await load(); }
    catch (cause) { toast.error(failure(cause, "Could not revoke the key")); } finally { setBusy(false); setRevoking(null); }
  }

  async function roll(keyName: string) {
    setBusy(true);
    try {
      const key = await api.rollGatewayKey(keyName);
      setCreated(key); setCreating(false);
      toast.success(`Key ${key.name} rolled. Copy the new secret now.`);
      await load();
    } catch (cause) { toast.error(failure(cause, "Could not roll the key")); } finally { setBusy(false); setRolling(null); }
  }

  const {sorted, sort, toggle} = useSort<GatewayKeyList["keys"][number], "name" | "created" | "used">(keys ?? [], {key: "name", descending: false}, {
    name: key => key.name.toLocaleLowerCase(),
    created: key => key.created_at ?? null,
    used: key => key.last_used_at ?? null,
  });

  return <div className="fleet-page keys-page">
    <PageHeader title="API keys" description="Keys let apps call the models the fleet serves." actions={<button type="button" className="button" onClick={() => setCreating(value => !value)}>Create key</button>}/>
    {(creating || created) && <section className="fleet-enrollment" aria-labelledby="key-create-heading">
      <div><h2 id="key-create-heading">Create a key</h2><p>Leave models empty to allow every model. CLI: <code>vonkctl key create &lt;name&gt;</code>. Rolling a key replaces its secret: <code>vonkctl key roll &lt;name&gt;</code></p></div>
      <form onSubmit={event => void create(event)}>
        <label htmlFor="key-name">Key name</label><input id="key-name" required maxLength={256} value={name} onChange={event => setName(event.currentTarget.value)} placeholder="e.g. laptop"/>
        <label htmlFor="key-models">Models (optional, separated by spaces)</label><input id="key-models" value={models} onChange={event => setModels(event.currentTarget.value)}/>
        <label htmlFor="key-expires">Expires after (optional, e.g. 30d or 12h)</label><input id="key-expires" pattern="[1-9][0-9]{0,5}[smhd]" value={expires} onChange={event => setExpires(event.currentTarget.value)}/>
        <button type="submit" className="button" disabled={busy}>Create</button>
      </form>
      {created && <div className="fleet-enrollment-result">
        <p><strong>Key {created.name}.</strong> Copy it now and store it somewhere safe. It is shown only once and cannot be retrieved later.</p>
        <input readOnly aria-label="New key secret" value={created.key}/><CopyButton label="key" value={created.key}/>
        <button type="button" className="button secondary" onClick={() => setCreated(null)}>I have copied it</button>
      </div>}
    </section>}
    {error && <p role="alert">{error} <button type="button" className="button secondary" onClick={() => void load()}>Retry</button></p>}
    {!keys && !error && <SkeletonRows columns={6} label="Loading API keys"/>}
    {keys?.length === 0 && <EmptyState title="No API keys" description="A key lets an app call the models the fleet serves." action={{label: "Create key", onClick: () => setCreating(true)}}/>}
    {keys && keys.length > 0 && <section className="fleet-compact" aria-label="Client keys"><div className="fleet-table-scroll"><table>
      <caption className="sr-only">Client keys, never secrets</caption>
      <thead><tr><SortHeader column="name" label="Name" sort={sort} onSort={toggle}/><th scope="col">Models</th><SortHeader column="created" label="Created" sort={sort} onSort={toggle}/><SortHeader column="used" label="Last used" sort={sort} onSort={toggle}/><th scope="col">Expires</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
      <tbody>{sorted.map(key => <tr key={key.name}>
        <th scope="row">{key.name}</th><td>{key.models.length ? key.models.join(", ") : "All models"}</td>
        <td><Time value={key.created_at}/></td><td><Time value={key.last_used_at}/></td><td>{key.expires_at ? <Time value={key.expires_at}/> : "Never"}</td>
        <td><div className="button-row"><button type="button" className="button secondary" onClick={() => setRolling(key.name)}>Roll<span className="sr-only"> {key.name}</span></button><button type="button" className="button secondary" onClick={() => setRevoking(key.name)}>Revoke<span className="sr-only"> {key.name}</span></button></div></td>
      </tr>)}</tbody></table></div></section>}
    {rolling && <ConfirmDialog title={`Roll key ${rolling}?`} consequence="A new secret replaces this one and is shown once. Apps using the old secret stop working immediately." confirmLabel="Roll key" command={`vonkctl key roll ${rolling}`} busy={busy} onConfirm={() => void roll(rolling)} onCancel={() => setRolling(null)}/>}
    {revoking && <ConfirmDialog title={`Revoke key ${revoking}?`} consequence="Apps using this key stop working immediately. This cannot be undone." confirmLabel="Revoke key" typeName={revoking} command={`vonkctl key revoke ${revoking}`} busy={busy} onConfirm={() => void revoke(revoking)} onCancel={() => setRevoking(null)}/>}
  </div>;
}

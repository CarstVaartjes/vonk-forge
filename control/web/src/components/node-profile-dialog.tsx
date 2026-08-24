import {useEffect, useId, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent} from "react";
import type {ControlApi, VisualFleetNode} from "../api/types";
import {nodeDisplayName} from "../lib/fleet";
import {CopyButton} from "./copy-button";

function boundedError(value: unknown): string {
  const message = value instanceof Error ? value.message : "The friendly name could not be saved.";
  return message.length > 256 ? `${message.slice(0, 256)}…` : message;
}

export function NodeProfileDialog({
  api,
  node,
  onClose,
  onSaved,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  onClose(): void;
  onSaved(displayName: string): void;
}) {
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [displayName, setDisplayName] = useState(nodeDisplayName(node));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const normalized = displayName.trim();
  const valid = normalized.length > 0 && normalized.length <= 80 && !/[\x00-\x1f\x7f]/u.test(normalized);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    input.current?.select();
    return () => { document.body.style.overflow = previousOverflow; };
  }, []);

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && !saving) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled])") ?? [])];
    if (focusable.length === 0) return;
    const first = focusable[0]!;
    const last = focusable.at(-1)!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function save() {
    if (!valid || saving) return;
    setSaving(true);
    setError("");
    try {
      const identity = await api.updateNodeProfile(node.id, {display_name: normalized});
      onSaved(identity.display_name);
      onClose();
    } catch (value) {
      setError(boundedError(value));
      input.current?.focus();
    } finally {
      setSaving(false);
    }
  }

  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !saving) onClose(); }}>
    <div ref={dialog} className="library-action-dialog node-profile-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-busy={saving || undefined} onKeyDown={handleKeyDown}>
      <header><div><p className="fleet-kicker">Spark identity</p><h3 id={titleId}>Name this Spark</h3><p className="dialog-subtitle">Give operators a useful name without changing how the Spark authenticates or connects.</p></div><button type="button" className="icon-button" disabled={saving} onClick={onClose} aria-label="Close Spark identity editor">×</button></header>
      <form className="library-action-dialog-body" onSubmit={event => { event.preventDefault(); void save(); }}>
        <div className="profile-name-field"><label htmlFor={`${titleId}-name`}>Friendly name</label><input ref={input} id={`${titleId}-name`} value={displayName} maxLength={80} aria-describedby={`${titleId}-help`} onChange={event => setDisplayName(event.currentTarget.value)}/><small id={`${titleId}-help`}>Shown throughout Fleet, Library, and Activity. Up to 80 characters.</small></div>
        {error && <p className="dialog-error" role="alert">{error}</p>}
        <dl className="profile-identity-facts">
          <div><dt>Internal hostname</dt><dd><code>{node.hostname || "Not reported by this agent"}</code></dd></div>
          <div><dt>Management IP</dt><dd><code>{node.ip_address || "Not currently reported"}</code></dd></div>
          <div><dt>Immutable Spark ID</dt><dd><span className="technical-identifier"><code>{node.id}</code><CopyButton label="Spark ID" value={node.id}/></span></dd></div>
        </dl>
      </form>
      <footer><button type="button" className="button secondary" disabled={saving} onClick={onClose}>Cancel</button><button type="button" className="button" disabled={!valid || saving} onClick={() => void save()}>{saving ? "Saving…" : "Save friendly name"}</button></footer>
    </div>
  </div>;
}

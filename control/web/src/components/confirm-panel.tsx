import {useState, type FormEvent} from "react";

type ConfirmPanelProps = {
  title: string;
  consequence: string;
  confirmLabel: string;
  /** Irreversible actions ask the operator to type this name first. */
  typeName?: string;
  command?: string;
  busy?: boolean;
  onConfirm(): void;
  onCancel(): void;
};

export function ConfirmPanel({title, consequence, confirmLabel, typeName, command, busy = false, onConfirm, onCancel}: ConfirmPanelProps) {
  const [typed, setTyped] = useState("");
  const ready = !typeName || typed === typeName;
  function submit(event: FormEvent) {
    event.preventDefault();
    if (ready && !busy) onConfirm();
  }
  return <form className="confirm-panel" role="alertdialog" aria-modal="false" aria-labelledby="confirm-title" aria-describedby="confirm-consequence" onSubmit={submit} onKeyDown={event => { if (event.key === "Escape") onCancel(); }}>
    <h3 id="confirm-title">{title}</h3>
    <p id="confirm-consequence">{consequence}</p>
    {typeName && <label>Type <strong>{typeName}</strong> to confirm<input autoFocus value={typed} onChange={event => setTyped(event.currentTarget.value)} autoComplete="off"/></label>}
    {command && <p className="confirm-command">CLI: <code>{command}</code></p>}
    <div className="button-row"><button type="submit" className="button danger" disabled={!ready || busy}>{busy ? "Working…" : confirmLabel}</button><button type="button" className="button secondary" autoFocus={!typeName} onClick={onCancel}>Cancel</button></div>
  </form>;
}

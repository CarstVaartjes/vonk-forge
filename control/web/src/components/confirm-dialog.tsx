import {useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent} from "react";
import {createPortal} from "react-dom";
import {CopyButton} from "./copy-button";

type ConfirmDialogProps = {
  title: string;
  /** What will happen, in words. */
  consequence: string;
  confirmLabel: string;
  /** Irreversible actions ask the operator to type this name first. */
  typeName?: string;
  /** The `vonkctl` equivalent, shown copyable. */
  command?: string;
  busy?: boolean;
  onConfirm(): void;
  onCancel(): void;
};

const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

/** Modal confirmation: traps focus, Esc cancels, focus returns to whatever opened it. */
export function ConfirmDialog({title, consequence, confirmLabel, typeName, command, busy = false, onConfirm, onCancel}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  const ids = useId();
  const dialog = useRef<HTMLFormElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const ready = !typeName || typed === typeName;

  // The rest of the page cannot be reached (pointer, keyboard, assistive tech) while the dialog is open.
  // Notifications stay live so an outcome is never hidden behind the modal.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const disabled = [...document.body.children].filter((element): element is HTMLElement =>
      element instanceof HTMLElement && element !== backdrop.current && !element.inert && !element.classList.contains("toast-region"));
    for (const element of disabled) element.inert = true;
    const first = dialog.current?.querySelector<HTMLElement>(typeName ? "input" : "[data-cancel]");
    first?.focus();
    return () => {
      for (const element of disabled) element.inert = false;
      if (opener?.isConnected) opener.focus();
    };
  }, [typeName]);

  function keyDown(event: KeyboardEvent<HTMLFormElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!busy) onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const items = [...(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    if (items.length === 0) return;
    const first = items[0]!, last = items[items.length - 1]!;
    if (!dialog.current?.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
    else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (ready && !busy) onConfirm();
  }

  return createPortal(<div ref={backdrop} className="modal-backdrop">
    <form ref={dialog} className="modal" role="alertdialog" aria-modal="true" aria-labelledby={`${ids}-title`} aria-describedby={`${ids}-consequence`} onSubmit={submit} onKeyDown={keyDown}>
      <h2 id={`${ids}-title`}>{title}</h2>
      <p id={`${ids}-consequence`}>{consequence}</p>
      {typeName && <label>Type <strong>{typeName}</strong> to confirm<input value={typed} onChange={event => setTyped(event.currentTarget.value)} autoComplete="off"/></label>}
      {command && <div className="modal-command"><span>CLI</span><code>{command}</code><CopyButton label="command" value={command}/></div>}
      <div className="button-row">
        <button type="submit" className="button danger" disabled={!ready || busy}>{busy ? "Working…" : confirmLabel}</button>
        <button type="button" className="button secondary" data-cancel disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  </div>, document.body);
}

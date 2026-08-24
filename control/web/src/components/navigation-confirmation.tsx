import {useEffect, useRef} from "react";
import type {KeyboardEvent} from "react";

export function NavigationConfirmation({destination, onCancel, onDiscard}: {
  destination: string;
  onCancel(): void;
  onDiscard(): void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const keepEditing = useRef<HTMLButtonElement>(null);

  useEffect(() => { keepEditing.current?.focus(); }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>("button:not([disabled])") ?? [])];
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

  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onCancel(); }}>
    <div ref={dialog} className="library-action-dialog navigation-confirmation" role="alertdialog" aria-modal="true" aria-labelledby="unsaved-draft-title" aria-describedby="unsaved-draft-description" onKeyDown={handleKeyDown}>
      <header><div><p className="fleet-kicker">Unsaved recipe</p><h2 id="unsaved-draft-title">Discard this draft?</h2></div></header>
      <div className="library-action-dialog-body"><p id="unsaved-draft-description">Your custom recipe has unsaved changes. Discard them to {destination}, or keep editing to preserve the draft.</p></div>
      <footer><button ref={keepEditing} type="button" className="button secondary" onClick={onCancel}>Keep editing</button><button type="button" className="button danger" onClick={onDiscard}>Discard draft</button></footer>
    </div>
  </div>;
}

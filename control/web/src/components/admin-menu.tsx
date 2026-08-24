import {useEffect, useId, useRef, useState} from "react";
import type {KeyboardEvent, MouseEvent as ReactMouseEvent} from "react";

type AdminMenuProps = {
  logoutError: string;
  loggingOut: boolean;
  navigationLocked?: boolean;
  onNavigateToActivity(event: ReactMouseEvent<HTMLAnchorElement>): void;
  onLogout(): void;
  role: string;
  subject: string;
};

export function AdminMenu({
  logoutError,
  loggingOut,
  navigationLocked = false,
  onNavigateToActivity,
  onLogout,
  role,
  subject,
}: AdminMenuProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuId = useId();
  const menu = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const firstItem = menu.current?.querySelector<HTMLElement>("a[href]:not([aria-disabled='true']), button:not([disabled])");
    firstItem?.focus();
    function closeOnOutsidePointer(event: PointerEvent): void {
      if (!menu.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setMenuOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [menuOpen]);

  useEffect(() => {
    if (navigationLocked) setMenuOpen(false);
  }, [navigationLocked]);

  function handleMenuKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setMenuOpen(false);
      trigger.current?.focus();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = [...(menu.current?.querySelectorAll<HTMLElement>("a[href]:not([aria-disabled='true']), button:not([disabled])") ?? [])];
    if (items.length === 0) return;
    event.preventDefault();
    const current = items.indexOf(document.activeElement as HTMLElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" ? (current + 1) % items.length : (current - 1 + items.length) % items.length;
    items[next]?.focus();
  }

  return <section className="operator-identity" aria-label="Authenticated operator">
    <button
      ref={trigger}
      type="button"
      className="operator-summary"
      aria-controls={menuId}
      aria-expanded={menuOpen}
      disabled={navigationLocked}
      title={navigationLocked ? "Operator actions are unavailable while a change is applying" : undefined}
      onClick={() => setMenuOpen(open => !open)}
    >
      <span className="operator-avatar" aria-hidden="true">{subject.slice(0, 1).toUpperCase()}</span>
      <div>
        <strong>{subject}</strong>
        <span>{role}</span>
      </div>
      <span className="operator-disclosure-indicator" aria-hidden="true">⌄</span>
    </button>
    {menuOpen && <div ref={menu} id={menuId} role="group" aria-label="Operator actions" className="admin-menu-panel" onKeyDown={handleMenuKeyDown}>
      <a href="/activity" className="secondary-button" aria-disabled={navigationLocked || undefined} tabIndex={navigationLocked ? -1 : undefined} onClick={event => {
        if (navigationLocked) {
          event.preventDefault();
          return;
        }
        setMenuOpen(false);
        onNavigateToActivity(event);
      }}>Open Activity</a>
      <button type="button" className="logout" aria-disabled={loggingOut || navigationLocked || undefined} disabled={loggingOut || navigationLocked} onClick={onLogout}>{loggingOut ? "Signing out…" : "Logout"}</button>
      {logoutError && <p role="alert">{logoutError}</p>}
    </div>}
  </section>;
}

import {useId, useState} from "react";
import type {AuditResponse, AuditSummary} from "../api/types";

type AdminMenuProps = {
  environment: string;
  loadAudit(): Promise<AuditResponse>;
  logoutError: string;
  loggingOut: boolean;
  onLogout(): void;
  role: string;
  subject: string;
};

const MAX_AUDIT_EVENTS = 8;

export function AdminMenu({
  environment,
  loadAudit,
  logoutError,
  loggingOut,
  onLogout,
  role,
  subject,
}: AdminMenuProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState("");
  const [events, setEvents] = useState<AuditSummary[] | null>(null);
  const menuTitleId = useId();
  const auditTitleId = useId();
  const menuId = useId();

  async function openAudit(): Promise<void> {
    setAuditOpen(true);
    if (events || auditLoading) return;
    setAuditError("");
    setAuditLoading(true);
    try {
      const response = await loadAudit();
      setEvents(response.events.slice(0, MAX_AUDIT_EVENTS));
    } catch (error) {
      setAuditError(error instanceof Error ? error.message : "Unable to load audit log.");
    } finally {
      setAuditLoading(false);
    }
  }

  return <section className="operator-identity" aria-label="Authenticated operator">
    <button
      type="button"
      className="operator-summary"
      aria-controls={menuId}
      aria-expanded={menuOpen}
      onClick={() => setMenuOpen(open => !open)}
    >
      <span className="operator-avatar" aria-hidden="true">{subject.slice(0, 1).toUpperCase()}</span>
      <div>
        <strong>{subject}</strong>
        <span>{role}</span>
      </div>
      <span className="environment-badge">{environment}</span>
    </button>
    {menuOpen && <div id={menuId} role="dialog" aria-labelledby={menuTitleId} className="admin-menu-panel">
      <h2 id={menuTitleId}>Operator menu</h2>
      <div className="admin-menu-actions">
        <button type="button" className="secondary-button" onClick={() => void openAudit()}>Audit log</button>
        <button type="button" className="logout" disabled={loggingOut} onClick={onLogout}>{loggingOut ? "Signing out…" : "Logout"}</button>
      </div>
      {logoutError && <p role="alert">{logoutError}</p>}
    </div>}
    {auditOpen && <div className="audit-drawer" role="dialog" aria-labelledby={auditTitleId} aria-modal="false">
      <div className="audit-drawer-header">
        <h3 id={auditTitleId}>Audit log</h3>
        <button type="button" className="secondary-button" onClick={() => setAuditOpen(false)}>Close audit log</button>
      </div>
      {auditLoading && <p role="status">Loading audit log…</p>}
      {auditError && <p role="alert">{auditError}</p>}
      {!auditLoading && !auditError && events && <ul className="audit-event-list">
        {events.map(event => <li key={event.request_id} className="audit-event">
          <strong>{event.action}</strong>
          <div>{`Actor ${event.actor}`}</div>
          {event.targets.length > 0 && <small>{`Target ${event.targets[0]}`}</small>}
        </li>)}
      </ul>}
      {!auditLoading && !auditError && events?.length === 0 && <p>No audit events yet.</p>}
    </div>}
  </section>;
}

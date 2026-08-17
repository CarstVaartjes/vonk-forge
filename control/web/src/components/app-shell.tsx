import {useEffect, useRef, useState} from "react";
import type {MouseEvent, ReactNode} from "react";
import {CloseIcon, FleetIcon, LibraryIcon, MenuIcon} from "./icons";

export type AppRoute = "fleet" | "library";

type Operator = {
  environment: string;
  logoutError: string;
  loggingOut: boolean;
  onLogout(): void;
  role: string;
  subject: string;
};

type AppShellProps = {
  activeRoute: AppRoute;
  children: ReactNode;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute): void;
  operator?: Operator;
};

export function AppShell({activeRoute, children, onNavigate, operator}: AppShellProps) {
  const [navigationOpen, setNavigationOpen] = useState(false);
  const mainContent = useRef<HTMLElement>(null);
  const previousRoute = useRef(activeRoute);
  useEffect(() => {
    if (previousRoute.current === activeRoute) return;
    previousRoute.current = activeRoute;
    mainContent.current?.focus();
  }, [activeRoute]);
  function navigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute) {
    setNavigationOpen(false);
    onNavigate(event, route);
  }
  return <div className="shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="app-sidebar">
      <div className="brand">
        <span className="mark" aria-hidden="true">VF</span>
        <div><strong>Vonk Forge</strong><small>Cluster control</small></div>
      </div>
      <button type="button" className="navigation-toggle" aria-controls="shell-navigation" aria-expanded={navigationOpen} aria-label={`${navigationOpen ? "Close" : "Open"} system navigation`} onClick={() => setNavigationOpen(open => !open)}>
        {navigationOpen ? <CloseIcon/> : <MenuIcon/>}
      </button>
      <div id="shell-navigation" className={`shell-navigation${navigationOpen ? " is-open" : ""}`}>
        <nav aria-label="Primary">
          <p className="nav-label">Workspace</p>
          <a href="/fleet" className="nav-link nav-link-primary" aria-current={activeRoute === "fleet" ? "page" : undefined} onClick={event => navigate(event, "fleet")}><FleetIcon/><span>Fleet</span></a>
          <a href="/library" className="nav-link nav-link-primary" aria-current={activeRoute === "library" ? "page" : undefined} onClick={event => navigate(event, "library")}><LibraryIcon/><span>Library</span></a>
        </nav>
        <div className="sidebar-footer">
          {operator && <section className="operator-identity" aria-label="Authenticated operator">
            <div className="operator-summary"><span className="operator-avatar" aria-hidden="true">{operator.subject.slice(0, 1).toUpperCase()}</span><div><strong>{operator.subject}</strong><span>{operator.role}</span></div></div>
            <span className="environment-badge">{operator.environment}</span>
            <button type="button" className="logout" disabled={operator.loggingOut} onClick={operator.onLogout}>{operator.loggingOut ? "Signing out…" : "Logout"}</button>
            {operator.logoutError && <p role="alert">{operator.logoutError}</p>}
          </section>}
          <small className="authority-note">Local database authority</small>
        </div>
      </div>
    </aside>
    <main ref={mainContent} id="main-content" tabIndex={-1}><div className="content-frame">{children}</div></main>
  </div>;
}

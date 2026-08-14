import {useState} from "react";
import type {MouseEvent, ReactNode} from "react";
import {ActivityIcon, ChevronIcon, CloseIcon, FleetIcon, LibraryIcon, MenuIcon, SystemIcon} from "./icons";

export type AppRoute = "fleet" | "agents" | "profiles" | "models" | "catalog" | "packages" | "deployments" | "updates" | "jobs" | "audit";

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

type RouteLink = {label: string; route: AppRoute};

const activityRoutes: RouteLink[] = [
  {label: "Deployments", route: "deployments"},
  {label: "Updates", route: "updates"},
  {label: "Jobs", route: "jobs"},
  {label: "Audit", route: "audit"},
];

const systemRoutes: RouteLink[] = [
  {label: "Agents", route: "agents"},
  {label: "Profiles", route: "profiles"},
  {label: "Models", route: "models"},
  {label: "Packages", route: "packages"},
];

function RouteAnchor({activeRoute, item, onNavigate}: {activeRoute: AppRoute; item: RouteLink; onNavigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute): void}) {
  const current = activeRoute === item.route;
  return <a href={`/${item.route}`} className="nav-link" aria-current={current ? "page" : undefined} onClick={event => onNavigate(event, item.route)}><span>{item.label}</span></a>;
}

function NavGroup({activeRoute, icon, label, onNavigate, routes}: {activeRoute: AppRoute; icon: ReactNode; label: string; onNavigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute): void; routes: RouteLink[]}) {
  const hasCurrent = routes.some(item => item.route === activeRoute);
  return <details className="nav-group" open={hasCurrent}>
    <summary>{icon}<span>{label}</span><ChevronIcon className="nav-chevron"/></summary>
    <div className="nav-group-links">{routes.map(item => <RouteAnchor key={item.route} activeRoute={activeRoute} item={item} onNavigate={onNavigate}/>)}</div>
  </details>;
}

export function AppShell({activeRoute, children, onNavigate, operator}: AppShellProps) {
  const [navigationOpen, setNavigationOpen] = useState(false);
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
          <a href="/catalog" className="nav-link nav-link-primary" aria-current={activeRoute === "catalog" ? "page" : undefined} onClick={event => navigate(event, "catalog")}><LibraryIcon/><span>Library</span></a>
          <NavGroup activeRoute={activeRoute} icon={<ActivityIcon/>} label="Activity" onNavigate={navigate} routes={activityRoutes}/>
          <NavGroup activeRoute={activeRoute} icon={<SystemIcon/>} label="System" onNavigate={navigate} routes={systemRoutes}/>
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
    <main id="main-content" tabIndex={-1}><div className="content-frame">{children}</div></main>
  </div>;
}

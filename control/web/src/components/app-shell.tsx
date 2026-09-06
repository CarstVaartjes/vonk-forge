import {useEffect, useRef} from "react";
import type {MouseEvent, ReactNode} from "react";
import {AdminMenu} from "./admin-menu";
import {FleetIcon, LibraryIcon} from "./icons";

export type AppRoute = "fleet" | "library" | "activity";

type Operator = {
  logoutError: string;
  loggingOut: boolean;
  onLogout(): void;
  role: string;
  subject: string;
};

type AppShellProps = {
  activeRoute?: AppRoute;
  children: ReactNode;
  navigationKey?: string;
  navigationLocked?: boolean;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute): boolean | void;
  operator?: Operator;
};

export function AppShell({activeRoute, children, navigationKey = activeRoute, navigationLocked = false, onNavigate, operator}: AppShellProps) {
  const mainContent = useRef<HTMLElement>(null);
  const previousNavigationKey = useRef(navigationKey);
  useEffect(() => {
    if (previousNavigationKey.current === navigationKey) return;
    previousNavigationKey.current = navigationKey;
    mainContent.current?.focus();
  }, [navigationKey]);
  function navigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute) {
    if (navigationLocked) {
      event.preventDefault();
      return;
    }
    onNavigate(event, route);
  }
  const disabledLinkProps = navigationLocked ? {"aria-disabled": true as const, tabIndex: -1} : {};
  return <div className="shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <header className="app-header">
      <div className="brand">
        <span className="mark" aria-hidden="true">VF</span>
        <div><strong>Vonk Forge</strong><small>Cluster control</small></div>
      </div>
      <nav className="primary-navigation" aria-label="Primary">
        <a href="/fleet" className="nav-link nav-link-primary" aria-current={activeRoute === "fleet" ? "page" : undefined} {...disabledLinkProps} onClick={event => navigate(event, "fleet")}><FleetIcon/><span>Fleet</span></a>
        <a href="/library" className="nav-link nav-link-primary" aria-current={activeRoute === "library" ? "page" : undefined} {...disabledLinkProps} onClick={event => navigate(event, "library")}><LibraryIcon/><span>Library</span></a>
      </nav>
      <div className="header-utility">
        <span className="authority-note">Local Controller</span>
        {operator && <AdminMenu {...operator} navigationLocked={navigationLocked} onNavigateToActivity={event => navigate(event, "activity")}/>}
      </div>
    </header>
    <main ref={mainContent} id="main-content" tabIndex={-1}><div className="content-frame">{children}</div></main>
  </div>;
}

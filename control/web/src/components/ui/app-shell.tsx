import { useEffect, useRef } from "react";
import type { MouseEvent, ReactNode } from "react";
import { AdminMenu } from "./admin-menu";
import type { CliTokenDownload } from "../../api/types";
import type { LiveStatus } from "../../hooks/fleet-stream-state";
import { BrandMark } from "./brand-mark";
import { StatusPill } from "./status-pill";
import { ThemeToggle } from "./theme-toggle";
import { ActivityIcon, FleetIcon, LibraryIcon, WorkloadsIcon } from "./icons";

const LIVE_TONES = {
  live: "ok",
  delayed: "warn",
  reconnecting: "neutral",
  stale: "warn",
  disconnected: "warn",
} as const;

/** The routed pages: the one list the shell, the app and the accessibility guard read. */
export const appRoutes = ["overview", "workloads", "library", "keys", "activity"] as const;
export type AppRoute = (typeof appRoutes)[number];

type Operator = {
  onDownloadCliToken(): Promise<CliTokenDownload>;
  logoutError: string;
  loggingOut: boolean;
  onLogout(): void;
  operatorRole: string;
  subject: string;
};

type AppShellProps = {
  activeRoute?: AppRoute | undefined;
  children: ReactNode;
  footer?: ReactNode;
  live?: LiveStatus | null;
  navigationKey?: string;
  navigationLocked?: boolean;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute): void;
  operator?: Operator | undefined;
};

export function AppShell({
  activeRoute,
  children,
  footer,
  live,
  navigationKey = activeRoute,
  navigationLocked = false,
  onNavigate,
  operator,
}: AppShellProps) {
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
  const disabledLinkProps = navigationLocked
    ? { "aria-disabled": true as const, tabIndex: -1 }
    : {};
  return (
    <div className="shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <header className="app-header">
        <a
          className="brand"
          href="/overview"
          aria-label="Vonk Forge Controller, Overview"
          {...disabledLinkProps}
          onClick={(event) => navigate(event, "overview")}
        >
          <BrandMark />
          <div className="brand-text">
            <strong>Vonk Forge</strong>
            <small>Controller</small>
          </div>
        </a>
        <nav className="primary-navigation" aria-label="Primary">
          <a
            href="/overview"
            className="nav-link nav-link-primary"
            aria-current={activeRoute === "overview" ? "page" : undefined}
            {...disabledLinkProps}
            onClick={(event) => navigate(event, "overview")}
          >
            <FleetIcon />
            <span>Overview</span>
          </a>
          <a
            href="/workloads"
            className="nav-link nav-link-primary"
            aria-current={activeRoute === "workloads" ? "page" : undefined}
            {...disabledLinkProps}
            onClick={(event) => navigate(event, "workloads")}
          >
            <WorkloadsIcon />
            <span>Workloads</span>
          </a>
          <a
            href="/library"
            className="nav-link nav-link-primary"
            aria-current={activeRoute === "library" ? "page" : undefined}
            {...disabledLinkProps}
            onClick={(event) => navigate(event, "library")}
          >
            <LibraryIcon />
            <span>Library</span>
          </a>
          <a
            href="/activity"
            className="nav-link nav-link-primary"
            aria-current={activeRoute === "activity" ? "page" : undefined}
            {...disabledLinkProps}
            onClick={(event) => navigate(event, "activity")}
          >
            <ActivityIcon />
            <span>Activity</span>
          </a>
        </nav>
        <div className="header-utility">
          {live && <StatusPill tone={LIVE_TONES[live]}>{live}</StatusPill>}
          <span className="authority-note">Local Controller</span>
          <ThemeToggle />
          {operator && (
            <AdminMenu {...operator} navigationLocked={navigationLocked} onNavigate={navigate} />
          )}
        </div>
      </header>
      <main ref={mainContent} id="main-content" tabIndex={-1}>
        <div className="content-frame">
          {children}
          {footer}
        </div>
      </main>
    </div>
  );
}

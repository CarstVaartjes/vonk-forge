import {useEffect, useRef, useState} from "react";
import type {KeyboardEvent, MouseEvent, ReactNode} from "react";
import {AdminMenu} from "./admin-menu";
import {CloseIcon, FleetIcon, LibraryIcon, MenuIcon} from "./icons";

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
  const [navigationOpen, setNavigationOpen] = useState(false);
  const mainContent = useRef<HTMLElement>(null);
  const navigation = useRef<HTMLDivElement>(null);
  const navigationToggle = useRef<HTMLButtonElement>(null);
  const previousNavigationKey = useRef(navigationKey);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const desktop = window.matchMedia("(min-width: 865px)");
    const closeOnDesktop = (event: MediaQueryListEvent) => {
      if (event.matches) setNavigationOpen(false);
    };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);
  useEffect(() => {
    if (previousNavigationKey.current === navigationKey) return;
    previousNavigationKey.current = navigationKey;
    setNavigationOpen(false);
    mainContent.current?.focus();
  }, [navigationKey]);
  useEffect(() => {
    if (!navigationOpen) return;
    document.body.classList.add("shell-navigation-open");
    const firstLink = navigation.current?.querySelector<HTMLElement>("a[href]:not([aria-disabled='true'])");
    firstLink?.focus();
    const closeOutside = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || navigation.current?.contains(target) || navigationToggle.current?.contains(target)) return;
      if (target instanceof Element && target.closest("[role='dialog'], [role='alertdialog']")) return;
      setNavigationOpen(false);
      queueMicrotask(() => navigationToggle.current?.focus());
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      document.body.classList.remove("shell-navigation-open");
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [navigationOpen]);

  function closeNavigation(restoreFocus = true) {
    setNavigationOpen(false);
    if (restoreFocus) queueMicrotask(() => navigationToggle.current?.focus());
  }

  function handleNavigationKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!navigationOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeNavigation();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...(navigation.current?.querySelectorAll<HTMLElement>("a[href]:not([aria-disabled='true']), button:not([disabled]), summary") ?? [])];
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
  function navigate(event: MouseEvent<HTMLAnchorElement>, route: AppRoute) {
    if (navigationLocked) {
      event.preventDefault();
      return;
    }
    const navigationAccepted = onNavigate(event, route);
    if (navigationAccepted !== false) setNavigationOpen(false);
  }
  const disabledLinkProps = navigationLocked ? {"aria-disabled": true as const, tabIndex: -1} : {};
  return <div className="shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="app-sidebar">
      <div className="brand">
        <span className="mark" aria-hidden="true">VF</span>
        <div><strong>Vonk Forge</strong><small>Cluster control</small></div>
      </div>
      <button ref={navigationToggle} type="button" className="navigation-toggle" aria-controls="shell-navigation" aria-expanded={navigationOpen} aria-label={`${navigationOpen ? "Close" : "Open"} system navigation`} aria-hidden={navigationOpen || undefined} tabIndex={navigationOpen ? -1 : undefined} onClick={() => navigationOpen ? closeNavigation() : setNavigationOpen(true)}>
        {navigationOpen ? <CloseIcon/> : <MenuIcon/>}
      </button>
      {navigationOpen && <div className="shell-navigation-scrim" aria-hidden="true"/>}
      <div
        ref={navigation}
        id="shell-navigation"
        className={`shell-navigation${navigationOpen ? " is-open" : ""}`}
        role={navigationOpen ? "dialog" : undefined}
        aria-modal={navigationOpen || undefined}
        aria-labelledby={navigationOpen ? "shell-navigation-title" : undefined}
        onKeyDown={handleNavigationKeyDown}
      >
        {navigationOpen && <header className="shell-navigation-header">
          <strong id="shell-navigation-title">Navigation</strong>
          <button type="button" className="shell-navigation-close" aria-label="Close system navigation" onClick={() => closeNavigation()}><CloseIcon/></button>
        </header>}
        <nav aria-label="Primary">
          <p className="nav-label">Workspace</p>
          <a href="/fleet" className="nav-link nav-link-primary" aria-current={activeRoute === "fleet" ? "page" : undefined} {...disabledLinkProps} onClick={event => navigate(event, "fleet")}><FleetIcon/><span>Fleet</span></a>
          <a href="/library" className="nav-link nav-link-primary" aria-current={activeRoute === "library" ? "page" : undefined} {...disabledLinkProps} onClick={event => navigate(event, "library")}><LibraryIcon/><span>Library</span></a>
        </nav>
        <div className="sidebar-footer">
          {operator && <AdminMenu {...operator} navigationLocked={navigationLocked} onNavigateToActivity={event => navigate(event, "activity")}/>}
          <small className="authority-note">Local database authority</small>
        </div>
      </div>
    </aside>
    <main ref={mainContent} id="main-content" tabIndex={-1} inert={navigationOpen || undefined} aria-hidden={navigationOpen || undefined}><div className="content-frame">{children}</div></main>
  </div>;
}

import {useCallback, useEffect, useRef, useState} from "react";
import {useOptionalAuth} from "./auth";
import type {ControlApi} from "./api/types";
import {AppShell} from "./components/app-shell";
import type {AppRoute} from "./components/app-shell";
import {ActivityPage} from "./pages/activity";
import {FleetPage} from "./pages/fleet";
import {LibraryPage} from "./pages/library";

const pages: AppRoute[] = ["fleet", "library", "activity"];
function pageTitle(pathname: string): string {
  if (/^\/library\/recipes\//.test(pathname)) return "Recipe · Library · Vonk Forge";
  if (/^\/library(?:\/|$)/.test(pathname)) return "Library · Vonk Forge";
  if (pathname === "/activity") return "Activity · Vonk Forge";
  if (pathname === "/" || pathname === "/fleet") return "Fleet · Vonk Forge";
  return "Page not found · Vonk Forge";
}

function currentPage(pathname = location.pathname): AppRoute | undefined {
  const value = pathname.replace(/^\//, "");
  if (value === "" || value === "fleet") return "fleet";
  if (/^library(?:\/|$)/.test(value)) return "library";
  return pages.includes(value as AppRoute) ? value as AppRoute : undefined;
}

export function App({api}: {api: ControlApi}) {
  const auth = useOptionalAuth();
  const [url, setUrl] = useState(`${location.pathname}${location.search}`);
  const activeUrl = useRef(url);
  const [navigationLocked, setNavigationLockedState] = useState(false);
  const navigationLockedRef = useRef(false);
  const setNavigationLocked = useCallback((locked: boolean) => {
    navigationLockedRef.current = locked;
    setNavigationLockedState(locked);
  }, []);
  const pathname = new URL(url, location.origin).pathname;
  const page = currentPage(pathname);
  useEffect(() => { document.title = pageTitle(pathname); }, [pathname]);
  useEffect(() => {
    const listener = () => {
      if (navigationLockedRef.current) {
        history.pushState(null, "", activeUrl.current);
        setUrl(activeUrl.current);
        return;
      }
      const nextUrl = `${location.pathname}${location.search}`;
      activeUrl.current = nextUrl;
      setUrl(nextUrl);
    };
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!navigationLockedRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    addEventListener("popstate", listener);
    addEventListener("beforeunload", warnBeforeUnload);
    return () => {
      removeEventListener("popstate", listener);
      removeEventListener("beforeunload", warnBeforeUnload);
    };
  }, []);

  const setNavigationBusy = useCallback((busy: boolean) => {
    setNavigationLocked(busy);
  }, [setNavigationLocked]);

  function requestNavigation(perform: () => void): boolean {
    if (navigationLockedRef.current) return false;
    perform();
    return true;
  }

  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: AppRoute) {
    event.preventDefault();
    const nextPath = `/${target}`;
    return requestNavigation(() => {
      history.pushState(null, "", nextPath);
      activeUrl.current = nextPath;
      setUrl(nextPath);
    });
  }

  function navigatePath(event: React.MouseEvent<HTMLAnchorElement>, nextPath: string) {
    event.preventDefault();
    requestNavigation(() => {
      history.pushState(null, "", nextPath);
      activeUrl.current = nextPath;
      setUrl(nextPath);
    });
  }

  function navigateUrl(nextUrl: string, replace = false) {
    requestNavigation(() => {
      replace ? history.replaceState(null, "", nextUrl) : history.pushState(null, "", nextUrl);
      activeUrl.current = nextUrl;
      setUrl(nextUrl);
    });
  }
  const content = page ? {
    fleet: <FleetPage api={api} onBusyChange={setNavigationLocked}/>,
    library: <LibraryPage api={api} path={url} onNavigate={navigatePath} onNavigatePath={navigateUrl} onBusyChange={setNavigationBusy}/>,
    activity: <ActivityPage api={api}/>,
  }[page] : <section className="fleet-empty route-not-found" aria-labelledby="not-found-heading">
    <p className="fleet-kicker">Unknown workspace</p>
    <h1 id="not-found-heading" ref={element => { if (element) queueMicrotask(() => element.focus()); }} tabIndex={-1}>Page not found</h1>
    <p>This address does not match a Vonk Forge workspace. Choose a safe destination below.</p>
    <div className="button-row"><a className="button" href="/fleet" onClick={event => navigate(event, "fleet")}>Go to Fleet</a><a className="button secondary" href="/library" onClick={event => navigate(event, "library")}>Go to Library</a></div>
  </section>;

  return <><AppShell
    activeRoute={page}
    navigationKey={pathname}
    navigationLocked={navigationLocked}
    onNavigate={navigate}
    operator={auth ? {
      logoutError: auth.logoutError,
      loggingOut: auth.loggingOut,
      onLogout: () => requestNavigation(() => { void auth.logout(); }),
      role: "Administrator",
      subject: auth.session.subject,
    } : undefined}
  >{content}</AppShell></>;
}

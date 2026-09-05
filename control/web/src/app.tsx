import {useCallback, useEffect, useRef, useState} from "react";
import {useOptionalAuth} from "./auth";
import type {CatalogApi, ControlApi} from "./api/types";
import {AppShell} from "./components/app-shell";
import type {AppRoute} from "./components/app-shell";
import {NavigationConfirmation} from "./components/navigation-confirmation";
import {CustomRecipeBuilderPage, discardStoredCustomRecipeDraft} from "./pages/custom-recipe-builder";
import {ActivityPage} from "./pages/activity";
import {FleetPage} from "./pages/fleet";
import {LibraryPage} from "./pages/library";

const pages: AppRoute[] = ["fleet", "library", "activity"];
type PendingNavigation = {destination: string; perform(): void; restoreFocus?: HTMLElement | null};

function pageTitle(pathname: string): string {
  if (pathname === "/library/create") return "Create custom recipe · Vonk Forge";
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
  const draftDirtyRef = useRef(false);
  const [pendingNavigation, setPendingNavigation] = useState<PendingNavigation>();
  const setNavigationLocked = useCallback((locked: boolean) => {
    navigationLockedRef.current = locked;
    setNavigationLockedState(locked);
  }, []);
  const setDraftDirty = useCallback((dirty: boolean) => { draftDirtyRef.current = dirty; }, []);
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
      if (draftDirtyRef.current) {
        history.pushState(null, "", activeUrl.current);
        setUrl(activeUrl.current);
        setPendingNavigation({destination: "leave the recipe builder", perform: () => history.back()});
        return;
      }
      const nextUrl = `${location.pathname}${location.search}`;
      activeUrl.current = nextUrl;
      setUrl(nextUrl);
    };
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!navigationLockedRef.current && !draftDirtyRef.current) return;
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

  function requestNavigation(destination: string, perform: () => void): boolean {
    if (navigationLockedRef.current) return false;
    if (draftDirtyRef.current) {
      setPendingNavigation({
        destination,
        perform,
        restoreFocus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
      });
      return false;
    }
    perform();
    return true;
  }

  function cancelPendingNavigation() {
    const trigger = pendingNavigation?.restoreFocus;
    setPendingNavigation(undefined);
    queueMicrotask(() => trigger?.focus());
  }

  function discardAndContinue() {
    const pending = pendingNavigation;
    if (!pending) return;
    draftDirtyRef.current = false;
    discardStoredCustomRecipeDraft();
    setPendingNavigation(undefined);
    pending.perform();
  }

  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: AppRoute) {
    event.preventDefault();
    const nextPath = `/${target}`;
    return requestNavigation(`go to ${target.charAt(0).toUpperCase()}${target.slice(1)}`, () => {
      history.pushState(null, "", nextPath);
      activeUrl.current = nextPath;
      setUrl(nextPath);
    });
  }

  function navigatePath(event: React.MouseEvent<HTMLAnchorElement>, nextPath: string) {
    event.preventDefault();
    requestNavigation("open another Library page", () => {
      history.pushState(null, "", nextPath);
      activeUrl.current = nextPath;
      setUrl(nextPath);
    });
  }

  function navigateUrl(nextUrl: string, replace = false) {
    requestNavigation(nextUrl.startsWith("/library") ? "return to the Library" : "leave the recipe builder", () => {
      replace ? history.replaceState(null, "", nextUrl) : history.pushState(null, "", nextUrl);
      activeUrl.current = nextUrl;
      setUrl(nextUrl);
    });
  }
  const content = page ? {
    fleet: <FleetPage api={api} onBusyChange={setNavigationLocked}/>,
    library: pathname === "/library/create"
        ? <CustomRecipeBuilderPage api={api as ControlApi & CatalogApi} onNavigate={navigateUrl} onBusyChange={setNavigationBusy} onDirtyChange={setDraftDirty}/>
      : <LibraryPage api={api} path={url} onNavigate={navigatePath} onNavigatePath={navigateUrl} onBusyChange={setNavigationBusy}/>,
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
      onLogout: () => requestNavigation("sign out", () => { void auth.logout(); }),
      role: "Administrator",
      subject: auth.session.subject,
    } : undefined}
  >{content}</AppShell>{pendingNavigation && <NavigationConfirmation destination={pendingNavigation.destination} onCancel={cancelPendingNavigation} onDiscard={discardAndContinue}/>}</>;
}

import {useCallback, useEffect, useRef, useState} from "react";
import {useOptionalAuth} from "./auth";
import type {CatalogApi, ControlApi} from "./api/types";
import {AppShell} from "./components/app-shell";
import type {AppRoute} from "./components/app-shell";
import {CustomRecipeBuilderPage} from "./pages/custom-recipe-builder";
import {ActivityPage} from "./pages/activity";
import {FleetPage} from "./pages/fleet";
import {LibraryPage} from "./pages/library";
import {PublicRecipeImportPage} from "./pages/public-recipe-import";

const pages: AppRoute[] = ["fleet", "library", "activity"];

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

  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: AppRoute) {
    event.preventDefault();
    if (navigationLockedRef.current) return;
    const nextPath = `/${target}`;
    history.pushState(null, "", nextPath);
    activeUrl.current = nextPath;
    setUrl(nextPath);
  }

  function navigatePath(event: React.MouseEvent<HTMLAnchorElement>, nextPath: string) {
    event.preventDefault();
    if (navigationLockedRef.current) return;
    history.pushState(null, "", nextPath);
    activeUrl.current = nextPath;
    setUrl(nextPath);
  }

  function navigateUrl(nextUrl: string, replace = false) {
    replace ? history.replaceState(null, "", nextUrl) : history.pushState(null, "", nextUrl);
    activeUrl.current = nextUrl;
    setUrl(nextUrl);
  }
  const content = page ? {
    fleet: <FleetPage api={api} onBusyChange={setNavigationLocked}/>,
    library: pathname === "/library/import"
      ? <PublicRecipeImportPage api={api as ControlApi & CatalogApi} url={url} onNavigate={navigateUrl} onBusyChange={setNavigationBusy}/>
      : pathname === "/library/create"
        ? <CustomRecipeBuilderPage api={api as ControlApi & CatalogApi} onNavigate={navigateUrl} onBusyChange={setNavigationBusy}/>
      : <LibraryPage api={api} path={pathname} onNavigate={navigatePath} onBusyChange={setNavigationBusy}/>,
    activity: <ActivityPage api={api}/>,
  }[page] : null;

  return <AppShell
    activeRoute={page}
    navigationLocked={navigationLocked}
    onNavigate={navigate}
    operator={auth ? {
      environment: "Development",
      logoutError: auth.logoutError,
      loggingOut: auth.loggingOut,
      onLogout: () => { if (!navigationLockedRef.current) void auth.logout(); },
      role: "Administrator",
      subject: auth.session.subject,
    } : undefined}
  >{content}</AppShell>;
}

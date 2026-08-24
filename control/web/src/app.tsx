import {useEffect, useState} from "react";
import {useOptionalAuth} from "./auth";
import type {CatalogApi, ControlApi} from "./api/types";
import {AppShell} from "./components/app-shell";
import type {AppRoute} from "./components/app-shell";
import {FleetPage} from "./pages/fleet";
import {LibraryPage} from "./pages/library";
import {PublicRecipeImportPage} from "./pages/public-recipe-import";

const pages: AppRoute[] = ["fleet", "library"];

function currentPage(pathname = location.pathname): AppRoute | undefined {
  const value = pathname.replace(/^\//, "");
  if (value === "" || value === "fleet") return "fleet";
  if (/^library(?:\/|$)/.test(value)) return "library";
  return pages.includes(value as AppRoute) ? value as AppRoute : undefined;
}

export function App({api}: {api: ControlApi}) {
  const auth = useOptionalAuth();
  const [url, setUrl] = useState(`${location.pathname}${location.search}`);
  const [navigationLocked, setNavigationLocked] = useState(false);
  const pathname = new URL(url, location.origin).pathname;
  const page = currentPage(pathname);
  useEffect(() => {
    const listener = () => setUrl(`${location.pathname}${location.search}`);
    addEventListener("popstate", listener);
    return () => removeEventListener("popstate", listener);
  }, []);

  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: AppRoute) {
    event.preventDefault();
    if (navigationLocked) return;
    const nextPath = `/${target}`;
    history.pushState(null, "", nextPath);
    setUrl(nextPath);
  }

  function navigatePath(event: React.MouseEvent<HTMLAnchorElement>, nextPath: string) {
    event.preventDefault();
    history.pushState(null, "", nextPath);
    setUrl(nextPath);
  }

  function navigateUrl(nextUrl: string, replace = false) {
    replace ? history.replaceState(null, "", nextUrl) : history.pushState(null, "", nextUrl);
    setUrl(nextUrl);
  }
  const content = page ? {
    fleet: <FleetPage api={api}/>,
    library: pathname === "/library/import"
      ? <PublicRecipeImportPage api={api as ControlApi & CatalogApi} url={url} onNavigate={navigateUrl} onBusyChange={setNavigationLocked}/>
      : <LibraryPage api={api} path={pathname} onNavigate={navigatePath}/>,
  }[page] : null;

  return <AppShell
    activeRoute={page}
    onNavigate={navigate}
    operator={auth ? {
      environment: "Development",
      loadAudit: () => api.audit(),
      logoutError: auth.logoutError,
      loggingOut: auth.loggingOut,
      onLogout: () => void auth.logout(),
      role: "Administrator",
      subject: auth.session.subject,
    } : undefined}
  >{content}</AppShell>;
}

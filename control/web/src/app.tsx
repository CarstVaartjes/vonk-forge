import {useEffect, useState} from "react";
import {useOptionalAuth} from "./auth";
import type {ControlApi} from "./api/types";
import {AppShell} from "./components/app-shell";
import type {AppRoute} from "./components/app-shell";
import {FleetPage} from "./pages/fleet";
import {LibraryPage} from "./pages/library";

const pages: AppRoute[] = ["fleet", "library"];

function currentPage(pathname = location.pathname): AppRoute {
  const value = pathname.replace(/^\//, "");
  if (/^library(?:\/|$)/.test(value)) return "library";
  return pages.includes(value as AppRoute) ? value as AppRoute : "fleet";
}

export function App({api}: {api: ControlApi}) {
  const auth = useOptionalAuth();
  const [path, setPath] = useState(location.pathname);
  const page = currentPage(path);
  useEffect(() => {
    const listener = () => setPath(location.pathname);
    addEventListener("popstate", listener);
    return () => removeEventListener("popstate", listener);
  }, []);

  function navigate(event: React.MouseEvent<HTMLAnchorElement>, target: AppRoute) {
    event.preventDefault();
    const nextPath = `/${target}`;
    history.pushState(null, "", nextPath);
    setPath(nextPath);
  }

  function navigatePath(event: React.MouseEvent<HTMLAnchorElement>, nextPath: string) {
    event.preventDefault();
    history.pushState(null, "", nextPath);
    setPath(nextPath);
  }
  const content = {
    fleet: <FleetPage api={api}/>,
    library: <LibraryPage api={api} path={path} onNavigate={navigatePath}/>,
  }[page];

  return <AppShell
    activeRoute={page}
    onNavigate={navigate}
    operator={auth ? {
      environment: "Development",
      logoutError: auth.logoutError,
      loggingOut: auth.loggingOut,
      onLogout: () => void auth.logout(),
      role: "Administrator",
      subject: auth.session.subject,
    } : undefined}
  >{content}</AppShell>;
}

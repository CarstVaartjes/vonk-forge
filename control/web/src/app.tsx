import {useEffect, useState} from "react";
import {useOptionalAuth} from "./auth";
import type {CatalogApi, ControlApi, WorkloadRunApi} from "./api/types";
import {AppShell} from "./components/app-shell";
import type {AppRoute} from "./components/app-shell";
import {FleetPage} from "./pages/fleet";
import {AgentsPage} from "./pages/agents";
import {ProfilesPage} from "./pages/profiles";
import {ModelsPage} from "./pages/models";
import {JobsPage} from "./pages/jobs";
import {AuditPage} from "./pages/audit";
import {UpdatesPage} from "./pages/updates";
import {PackagesPage} from "./pages/packages";
import {PackageCandidatePage} from "./pages/package-candidate";
import {DeploymentsPage} from "./pages/deployments";
import type {PackageApi} from "./pages/package-types";
import {CatalogPage} from "./pages/catalog";
import {RecipeEditorPage} from "./pages/recipe-editor";
import {WorkloadRunImportPage} from "./pages/workload-run-import";
import {RecipeSourcePage} from "./pages/recipe-source";
import {ClusterMappingPage} from "./pages/cluster-mapping";
import {LibraryPage} from "./pages/library";

const pages: AppRoute[] = ["fleet", "library", "agents", "profiles", "models", "catalog", "packages", "deployments", "updates", "jobs", "audit"];

function candidateId(): string | undefined {
  const match = /^\/packages\/([^/]+)$/.exec(location.pathname);
  if (!match) return undefined;
  try { return decodeURIComponent(match[1]); } catch { return undefined; }
}

function recipeId(): string | undefined {
  const match = /^\/catalog\/([^/]+)(?:\/(?:source|map))?$/.exec(location.pathname);
  if (!match || match[1] === "new") return undefined;
  try { return decodeURIComponent(match[1]); } catch { return undefined; }
}

function currentPage(pathname = location.pathname): AppRoute {
  const value = pathname.replace(/^\//, "");
  if (/^library(?:\/|$)/.test(value)) return "library";
  if (candidateId() || value === "packages") return "packages";
  if (value === "catalog/new" || value === "catalog/import/workload_run" || recipeId()) return "catalog";
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

  // W15's generated client supplies these package methods. This narrow cast
  // keeps W16 independent of generated declaration timing.
  const packageApi = api as unknown as PackageApi;
  const catalogApi = api as unknown as CatalogApi;
  const workloadRunApi = api as unknown as WorkloadRunApi;
  const selectedCandidate = candidateId();
  const selectedRecipe = recipeId();
  const catalogContent = location.pathname === "/catalog/new"
    ? <RecipeEditorPage api={catalogApi}/>
    : location.pathname === "/catalog/import/workload_run"
      ? <WorkloadRunImportPage api={workloadRunApi}/>
      : selectedRecipe && location.pathname.endsWith("/source")
        ? <RecipeSourcePage api={api as ControlApi & CatalogApi} recipeId={selectedRecipe}/>
        : selectedRecipe && location.pathname.endsWith("/map")
          ? <ClusterMappingPage api={api as ControlApi & CatalogApi} recipeId={selectedRecipe}/>
          : selectedRecipe
            ? <RecipeEditorPage api={catalogApi} recipeId={selectedRecipe}/>
            : <CatalogPage api={catalogApi}/>;
  const content = selectedCandidate
    ? <PackageCandidatePage api={packageApi} candidateId={selectedCandidate}/>
    : {
      fleet: <FleetPage api={api}/>,
      library: <LibraryPage api={api} path={path} onNavigate={navigatePath}/>,
      agents: <AgentsPage api={api}/>,
      profiles: <ProfilesPage api={api}/>,
      models: <ModelsPage api={api}/>,
      catalog: catalogContent,
      packages: <PackagesPage api={packageApi}/>,
      deployments: <DeploymentsPage api={packageApi}/>,
      updates: <UpdatesPage api={api}/>,
      jobs: <JobsPage api={api}/>,
      audit: <AuditPage api={api}/>,
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

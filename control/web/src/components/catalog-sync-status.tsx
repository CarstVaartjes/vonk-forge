import {useEffect, useState} from "react";
import type {CatalogSyncStatus, LibraryApi} from "../api/types";
import {Time} from "./time";
import {StatusPill} from "./status-pill";
import type {StatusTone} from "./status-pill";

const TONE: Record<CatalogSyncStatus["state"], StatusTone> = {current: "healthy", syncing: "info", partial: "warning", failed: "danger"};

/** Read-only: the Controller syncs the recipe library itself; this only reports the last run. */
export function CatalogSyncStatusLine({api}: {api: Pick<LibraryApi, "catalogSyncStatus">}) {
  const [status, setStatus] = useState<CatalogSyncStatus | null | undefined>();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!api.catalogSyncStatus) return;
    const controller = new AbortController();
    api.catalogSyncStatus(controller.signal).then(value => { if (!controller.signal.aborted) setStatus(value); })
      .catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Sync status unavailable"); });
    return () => controller.abort();
  }, [api]);
  if (error) return <p className="library-sync" role="status">Recipe library sync status unavailable: {error}</p>;
  if (status === undefined) return null;
  if (status === null) return <p className="library-sync" role="status">Recipe library sync has not run yet. The Controller syncs automatically.</p>;
  const at = status.completed_at ?? status.created_at;
  const problems = [...status.problems.map(item => `${item.code}: ${item.detail}`), ...(status.last_error ? [`${status.last_error.code}: ${status.last_error.detail}`] : [])];
  return <div className="library-sync" role="status">
    <p>
      <span>Recipe library sync </span><StatusPill tone={TONE[status.state]}>{status.state}</StatusPill>
      <> · last run <Time value={at}/></>
      {status.commit && <> · commit <code>{status.commit.slice(0, 12)}</code></>}
      {status.expected_commit && status.expected_commit !== status.commit && <> · expected commit <code>{status.expected_commit.slice(0, 12)}</code></>}
    </p>
    <p>{status.imported_count} imported · {status.updated_count} updated · {status.unchanged_count} unchanged · {status.skipped_count} skipped · {status.withdrawn_count} withdrawn of {status.total_count}</p>
    {(problems.length > 0 || status.stale_recipes.length > 0) && <details open={status.state !== "current"}><summary>{problems.length} problem{problems.length === 1 ? "" : "s"}{status.stale_recipes.length > 0 && ` · ${status.stale_recipes.length} recipe${status.stale_recipes.length === 1 ? "" : "s"} with stale installations or runs`}</summary><ul>{problems.map(item => <li key={item}>{item}</li>)}</ul></details>}
  </div>;
}

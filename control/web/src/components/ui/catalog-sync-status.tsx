import { formatWire } from "../../api/contract-numeric";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type { CatalogSyncStatus, LibraryApi, LibraryViewSnapshot } from "../../api/types";
import { lastSyncAt } from "../../lib/catalog-sync";
import { formatDate } from "../../lib/time";
import { Time } from "./time";
import { StatusPill } from "./status-pill";
import type { StatusTone } from "./status-pill";

const TONE: Record<CatalogSyncStatus["state"], StatusTone> = {
  current: "ok",
  syncing: "info",
  partial: "warn",
  failed: "bad",
};

/** Read-only: the Controller syncs the recipe library itself; this only reports the last sync.
 *
 * One quiet line ("Recipe library v5.2.0 · current · synced 2 hours ago"); the
 * commit, counters, problems and any `children` (storage observation) sit in a
 * disclosure that opens by itself when the sync is not current. */
export function CatalogSyncStatusLine({
  api,
  release,
  attention = false,
  children,
}: {
  api: Pick<LibraryApi, "catalogSyncStatus">;
  release?: LibraryViewSnapshot["library"];
  /** Open the details by themselves: something besides the sync needs a look. */
  attention?: boolean;
  children?: ReactNode;
}) {
  const [status, setStatus] = useState<CatalogSyncStatus | null | undefined>();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!api.catalogSyncStatus) return;
    const controller = new AbortController();
    api
      .catalogSyncStatus(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setStatus(value);
      })
      .catch((value) => {
        if (!controller.signal.aborted)
          setError(
            value instanceof Error ? value.message.slice(0, 256) : "Sync status unavailable",
          );
      });
    return () => controller.abort();
  }, [api]);
  const name = <>Recipe library{release ? ` v${release.version}` : ""}</>;
  const details = (open: boolean, body: ReactNode) => (
    <details open={open}>
      <summary>Details</summary>
      {release && (
        <p>
          Library release v{release.version}, updated {formatDate(new Date(release.updated_at))}.
        </p>
      )}
      {body}
      {children}
    </details>
  );
  if (error)
    return (
      <div className="library-sync" role="status">
        <p>{name} · sync status unavailable</p>
        {details(true, <p>{error}</p>)}
      </div>
    );
  if (status === undefined)
    return release || children ? (
      <div className="library-sync" role="status">
        <p>{name}</p>
        {details(attention, null)}
      </div>
    ) : null;
  if (status === null)
    return (
      <div className="library-sync" role="status">
        <p>{name} · sync has not run yet. The Controller syncs automatically.</p>
        {details(attention, null)}
      </div>
    );
  const at = lastSyncAt(status);
  const problems = [
    ...status.problems.map((item) => `${item.code}: ${item.detail}`),
    ...(status.last_error ? [`${status.last_error.code}: ${status.last_error.detail}`] : []),
  ];
  return (
    <div className="library-sync" role="status">
      <p>
        {name} <StatusPill tone={TONE[status.state]}>{status.state}</StatusPill> · synced{" "}
        <Time value={at} />
      </p>
      {details(
        status.state !== "current" || attention,
        <>
          <p>
            {status.commit && (
              <>
                Commit <code>{status.commit.slice(0, 12)}</code>
              </>
            )}
            {status.expected_commit && status.expected_commit !== status.commit && (
              <>
                {" "}
                · expected commit <code>{status.expected_commit.slice(0, 12)}</code>
              </>
            )}
          </p>
          <p>
            {formatWire(status.imported_count)} imported · {formatWire(status.updated_count)}{" "}
            updated · {formatWire(status.unchanged_count)} unchanged ·{" "}
            {formatWire(status.skipped_count)} skipped · {formatWire(status.withdrawn_count)}{" "}
            withdrawn of {formatWire(status.total_count)}
          </p>
          {(problems.length > 0 || status.stale_recipes.length > 0) && (
            <>
              <p>
                {problems.length} problem{problems.length === 1 ? "" : "s"}
                {status.stale_recipes.length > 0 &&
                  ` · ${status.stale_recipes.length} recipe${status.stale_recipes.length === 1 ? "" : "s"} with stale installations or runs`}
              </p>
              <ul>
                {problems.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </>
          )}
        </>,
      )}
    </div>
  );
}

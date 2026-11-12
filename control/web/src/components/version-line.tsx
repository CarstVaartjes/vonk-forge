import { useEffect, useState } from "react";
import type { CatalogSyncStatus, ControlApi, PlatformObservation } from "../api/types";
import { lastSyncAt } from "../lib/catalog-sync";
import { exactTime } from "../lib/time";
import { utcTime } from "../lib/utc-time";

const NOT_REPORTED = "Not reported";

/**
 * A quiet line of what is running: the Controller build, the recipe library version and the last
 * sync. Each fact is its own element and reads exactly as `vonkctl platform` and
 * `vonkctl recipe sync-status` print it (tests/fixtures/version-line.json). Each fact is read from
 * the API on its own, so one that fails reads "Not reported" and the others still show. It reads
 * again whenever `readKey` changes (every navigation), so it is never older than the page.
 */
export function VersionLine({
  api,
  readKey,
}: {
  api: Pick<ControlApi, "platformObservation" | "catalogSyncStatus">;
  readKey?: string;
}) {
  const [read, setRead] = useState(false);
  const [platform, setPlatform] = useState<PlatformObservation | null>(null);
  const [sync, setSync] = useState<CatalogSyncStatus | null | undefined>();
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void Promise.allSettled([api.platformObservation(signal), api.catalogSyncStatus(signal)]).then(
      ([platformRead, syncRead]) => {
        if (signal.aborted) return;
        setPlatform(platformRead.status === "fulfilled" ? platformRead.value : null);
        setSync(syncRead.status === "fulfilled" ? syncRead.value : undefined);
        setRead(true);
      },
    );
    return () => controller.abort();
  }, [api, readKey]);
  if (!read) return null;
  const build = platform?.api.source_sha;
  const syncedAt = sync && lastSyncAt(sync);
  const stamp = syncedAt ? utcTime(syncedAt) : null;
  return (
    <footer className="version-line" aria-label="Versions">
      <span>Controller build: {build ? <code>{build.slice(0, 12)}</code> : NOT_REPORTED}</span>
      <span>Library version: {sync?.library_version ?? NOT_REPORTED}</span>
      <span>
        Last sync:{" "}
        {sync === null ? (
          "none yet"
        ) : syncedAt && stamp ? (
          <>
            <time dateTime={syncedAt} title={exactTime(syncedAt) ?? undefined}>
              {stamp}
            </time>{" "}
            ({sync?.state})
          </>
        ) : (
          NOT_REPORTED
        )}
      </span>
    </footer>
  );
}

import { useEffect, useState } from "react";
import type { CatalogSyncStatus, ControlApi, PlatformObservation } from "../api/types";
import { Time } from "./time";

const NOT_REPORTED = "Not reported";

/**
 * A quiet line of what is running: the Controller build, the recipe library version and the last
 * sync. Each fact is read from the API on its own, so one that fails reads "Not reported" and the
 * others still show. The labels match `vonkctl platform` and `vonkctl recipe sync-status`.
 */
export function VersionLine({
  api,
}: {
  api: Pick<ControlApi, "platformObservation" | "catalogSyncStatus">;
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
  }, [api]);
  if (!read) return null;
  const build = platform?.api.source_sha;
  const syncedAt = sync && (sync.completed_at ?? sync.created_at);
  return (
    <footer className="version-line" aria-label="Versions">
      <span>Controller build {build ? <code>{build.slice(0, 12)}</code> : NOT_REPORTED}</span>
      <span>Library version {sync?.library_version ?? NOT_REPORTED}</span>
      <span>
        Last sync {sync === null ? "none yet" : syncedAt ? <Time value={syncedAt} /> : NOT_REPORTED}
      </span>
    </footer>
  );
}

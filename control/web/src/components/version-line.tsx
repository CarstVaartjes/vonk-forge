import { useEffect, useState } from "react";
import type { CatalogSyncStatus, ControlApi, PlatformObservation } from "../api/types";
import { Time } from "./time";

const NOT_REPORTED = "Not reported";

type Versions = {
  platform: PlatformObservation | null;
  sync: CatalogSyncStatus | null | undefined;
};

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
  const [versions, setVersions] = useState<Versions | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void Promise.allSettled([
      api.platformObservation(signal),
      api.catalogSyncStatus(signal),
    ]).then(([platform, sync]) => {
      if (signal.aborted) return;
      setVersions({
        platform: platform.status === "fulfilled" ? platform.value : null,
        sync: sync.status === "fulfilled" ? sync.value : undefined,
      });
    });
    return () => controller.abort();
  }, [api]);
  if (!versions) return null;
  const build = versions.platform?.api.source_sha;
  const syncedAt = versions.sync && (versions.sync.completed_at ?? versions.sync.created_at);
  return (
    <footer className="version-line" aria-label="Versions">
      <span>Controller build {build ? <code>{build.slice(0, 12)}</code> : NOT_REPORTED}</span>
      <span>Library version {versions.sync?.library_version ?? NOT_REPORTED}</span>
      <span>
        Last sync {versions.sync === null ? "none yet" : syncedAt ? <Time value={syncedAt} /> : NOT_REPORTED}
      </span>
    </footer>
  );
}

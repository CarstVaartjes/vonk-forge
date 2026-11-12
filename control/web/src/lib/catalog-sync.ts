import type { CatalogSyncStatus } from "../api/types";

/** The one rule for when a sync run happened: when it finished, else when it started. */
export function lastSyncAt(status: Pick<CatalogSyncStatus, "completed_at" | "created_at">) {
  return status.completed_at ?? status.created_at;
}

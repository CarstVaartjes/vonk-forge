import { addWire, compareWire, formatWire, type WireNumber } from "../api/contract-numeric";
import { ApiError } from "../api/errors";
import { LibraryNodeNamesProvider } from "../components/library/library-node-names";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ActivityFilters, JobDetail, OperationDetail } from "../api/types";
import { EmptyState } from "../components/ui/empty-state";
import { PageHeader } from "../components/ui/page-header";
import { SkeletonRows } from "../components/ui/skeleton";
import { relativeTime } from "../lib/time";
import { type ActivityStatus, activityStatus } from "./activity-status";
import {
  type ActivityApi,
  type ActivityRecord,
  activityCategory,
  activityTitle,
  canonicalRecord,
  failureText,
  sortActivityByAttention,
  sortActivityByTime,
  targetNameLookup,
  titleCase,
} from "./activity-records";
import {
  type ActivityView,
  OPERATION_STATES,
  REQUEST_ID,
  VIEW_PREFERENCE_KEY,
  readFilters,
  readStatus,
  readViewPreference,
  writeFilters,
} from "./activity-filters";
import { ActivityOverview, ActivityTable, ActivityTimeline } from "./activity-views";

export { relativeTime };
export function ActivityPage({ api, now = new Date() }: { api: ActivityApi; now?: Date }) {
  const [events, setEvents] = useState<ActivityRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [actor, setActor] = useState("");
  const [status, setStatus] = useState<ActivityStatus | "">(readStatus);
  const [filters, setFilters] = useState<ActivityFilters>(readFilters);
  const [requestText, setRequestText] = useState(() => readFilters().requestId ?? "");
  const [view, setView] = useState<ActivityView>(readViewPreference);
  const [sort, setSort] = useState<"recent" | "attention">("recent");
  const [targetNames, setTargetNames] = useState(new Map<string, string>());
  // The rarely used filters hide behind one button, open from the start when a link already set one.
  const [moreOpen, setMoreOpen] = useState(() => {
    const initial = readFilters();
    return Boolean(initial.state || initial.target || initial.requestId);
  });
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const canonicalIds = useRef(new Set<string>());
  const [canonicalCount, setCanonicalCount] = useState(0);
  const [canonicalTotal, setCanonicalTotal] = useState<WireNumber>(0);
  const [canonicalCursor, setCanonicalCursor] = useState<string | null>(null);
  const [canonicalAvailable, setCanonicalAvailable] = useState(true);
  const requestGeneration = useRef(0);

  useEffect(() => {
    // The link opened the page on a status; from here the filter is the page's own, like search.
    const params = new URLSearchParams(location.search);
    if (!params.has("status")) return;
    params.delete("status");
    history.replaceState(null, "", `${location.pathname}${params.size ? `?${params}` : ""}`);
  }, []);
  useEffect(() => {
    const generation = ++requestGeneration.current;
    let active = true;
    setLoading(true);
    setLoadingMore(false);
    setError("");
    const controller = new AbortController();
    void Promise.allSettled([
      api.operations(undefined, controller.signal, filters),
      api.visualFleet(controller.signal).catch(() => null),
    ])
      .then(([canonicalResult, fleetResult]) => {
        if (!active) return;
        const canonical = canonicalResult.status === "fulfilled" ? canonicalResult.value : null;
        const canonicalError =
          canonicalResult.status === "rejected"
            ? canonicalResult.reason instanceof Error
              ? canonicalResult.reason.message
              : "Unable to load operations."
            : "";
        setCanonicalAvailable(Boolean(canonical));
        if (!canonical) {
          setEvents(null);
          setError(`Unable to load activity. ${canonicalError}`);
          return;
        }
        if (canonical.operations == null || canonical.total == null) {
          setCanonicalAvailable(false);
          setError(
            canonical.projection_issue ||
              "Operation membership is currently unknown. Refresh to observe it again.",
          );
          return;
        }
        canonicalIds.current = new Set(canonical.operations.map((operation) => operation.id));
        setCanonicalCount(canonicalIds.current.size);
        setCanonicalTotal(canonical.total);
        setCanonicalCursor(canonical.next_cursor ?? null);
        const fleet = fleetResult.status === "fulfilled" ? fleetResult.value : null;
        const names = targetNameLookup(fleet);
        setTargetNames(names);
        setPaginationError("");
        setEvents(
          canonical.operations
            .filter(
              (operation, index, all) =>
                all.findIndex((item) => item.id === operation.id) === index,
            )
            .map((operation) => canonicalRecord(operation, names))
            .sort(sortActivityByTime),
        );
      })
      .catch((value) => {
        if (!active) return;
        setEvents(null);
        setError(value instanceof Error ? value.message : "Unable to prepare activity.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      if (requestGeneration.current === generation) requestGeneration.current += 1;
    };
  }, [api, attempt, filters]);

  async function loadMoreOperations(): Promise<void> {
    if (!canonicalCursor || loading || loadingMore) return;
    const generation = requestGeneration.current;
    setLoadingMore(true);
    setPaginationError("");
    const [canonicalResult] = await Promise.allSettled([
      api.operations(canonicalCursor, undefined, filters),
    ]);
    if (requestGeneration.current !== generation) return;
    if (
      "reason" in canonicalResult &&
      canonicalResult.reason instanceof ApiError &&
      canonicalResult.reason.status === 422
    ) {
      // A rejected cursor will never be accepted: drop it and reload page 1.
      setCanonicalCursor(null);
      setLoadingMore(false);
      setAttempt((value) => value + 1);
      return;
    }
    const additional: ActivityRecord[] = [];
    const errors: string[] = [];
    if (canonicalResult.status === "fulfilled" && canonicalResult.value) {
      const next = canonicalResult.value;
      if (next.operations == null || next.total == null) {
        setPaginationError(
          next.projection_issue || "Older operation membership is currently unknown. Try again.",
        );
        setLoadingMore(false);
        return;
      }
      for (const operation of next.operations) {
        if (canonicalIds.current.has(operation.id)) continue;
        canonicalIds.current.add(operation.id);
        additional.push(canonicalRecord(operation, targetNames));
      }
      setCanonicalCount(canonicalIds.current.size);
      setCanonicalTotal(next.total);
      setCanonicalCursor(next.next_cursor ?? null);
    } else if (canonicalResult.status === "rejected")
      errors.push(
        `Older operations could not be loaded. ${canonicalResult.reason instanceof Error ? canonicalResult.reason.message : "Try again."}`,
      );
    setEvents((current) => [...(current ?? []), ...additional].sort(sortActivityByTime));
    setPaginationError(errors.join(" "));
    setLoadingMore(false);
  }

  const categories = useMemo(
    () => [...new Set((events ?? []).map(activityCategory))].sort(),
    [events],
  );
  const actors = useMemo(
    () => [...new Set((events ?? []).map((event) => event.actor).filter(Boolean))].sort(),
    [events],
  );
  useEffect(() => {
    if (category && !categories.includes(category)) setCategory("");
    if (actor && !actors.includes(actor)) setActor("");
  }, [actor, actors, categories, category]);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (events ?? []).filter((event) => {
      if (category && activityCategory(event) !== category) return false;
      if (actor && event.actor !== actor) return false;
      if (status && activityStatus(event) !== status) return false;
      if (!normalized) return true;
      return [
        activityTitle(event),
        activityCategory(event),
        event.action,
        event.actor,
        event.request_id,
        failureText(event.operation?.failure),
        ...event.targets,
        ...(event.target_names ?? []),
      ].some((value) => value.toLocaleLowerCase().includes(normalized));
    });
  }, [actor, category, events, query, status]);
  const displayed = useMemo(
    () => [...filtered].sort(sort === "attention" ? sortActivityByAttention : sortActivityByTime),
    [filtered, sort],
  );

  const updateOperation = useCallback(
    (detail: JobDetail): void => {
      setEvents(
        (current) =>
          current
            ?.map((event) =>
              event.source === "job" && event.request_id === detail.id
                ? {
                    ...event,
                    action: `operation.${detail.kind}.${detail.state}`,
                    summary: detail.summary,
                    targets: detail.targets,
                    target_names: detail.targets.map((target) => targetNames.get(target) ?? ""),
                  }
                : event,
            )
            .sort(sortActivityByTime) ?? null,
      );
    },
    [targetNames],
  );

  const updateCanonicalOperation = useCallback(
    (detail: OperationDetail): void => {
      const record = canonicalRecord(detail, targetNames);
      const alreadyLoaded = canonicalIds.current.has(detail.id);
      canonicalIds.current.add(detail.id);
      setCanonicalCount(canonicalIds.current.size);
      if (!alreadyLoaded) setCanonicalTotal((current) => addWire(current, 1));
      setEvents((current) =>
        [
          ...(current ?? []).filter(
            (event) => !(event.source === "operation" && event.request_id === detail.id),
          ),
          record,
        ].sort(sortActivityByTime),
      );
    },
    [targetNames],
  );

  function chooseView(next: ActivityView): void {
    setView(next);
    try {
      localStorage.setItem(VIEW_PREFERENCE_KEY, next);
    } catch {
      /* Preferences are optional. */
    }
  }

  function changeFilters(next: ActivityFilters): void {
    setFilters(next);
    writeFilters(next);
  }

  function clearFilters(): void {
    setQuery("");
    setCategory("");
    setActor("");
    setStatus("");
    setRequestText("");
    changeFilters({});
  }

  const moreActive = [
    actor,
    filters.state,
    filters.target,
    filters.requestId,
    sort !== "recent",
  ].filter(Boolean).length;
  const filtering = Boolean(
    query.trim() ||
      category ||
      actor ||
      status ||
      filters.state ||
      filters.target ||
      filters.requestId,
  );
  const names = useMemo(() => Object.fromEntries(targetNames), [targetNames]);
  return (
    <LibraryNodeNamesProvider names={names}>
      <div className="activity-page">
        <PageHeader
          title="Activity"
          description="Meaningful control-plane changes, without technical identifiers by default."
          actions={
            <>
              <fieldset className="segmented group-reset" aria-label="Activity view">
                <button
                  type="button"
                  aria-pressed={view === "timeline"}
                  onClick={() => chooseView("timeline")}
                >
                  Timeline
                </button>
                <button
                  type="button"
                  aria-pressed={view === "table"}
                  onClick={() => chooseView("table")}
                >
                  Table
                </button>
              </fieldset>
              <button
                type="button"
                className="button secondary"
                disabled={loading || loadingMore}
                onClick={() => setAttempt((value) => value + 1)}
              >
                {loading && events ? "Refreshing…" : "Refresh activity"}
              </button>
            </>
          }
        />

        {events && events.length > 0 && (
          <ActivityOverview events={filtered} filtering={filtering} loadedCount={events.length} />
        )}

        {events && (
          <section className="library-pagination" aria-label="Activity history coverage">
            <p role="status">
              Showing {events.length} loaded {events.length === 1 ? "event" : "events"}.
              {canonicalCursor ? " Load older activity below." : ""}
            </p>
            <details className="activity-technical">
              <summary>History coverage</summary>
              <p>
                {canonicalAvailable
                  ? `Loaded ${canonicalCount} of ${formatWire(canonicalTotal)} operations`
                  : "Operations are unavailable"}
                . Summary counts and filters cover only these loaded records.
              </p>
            </details>
            {canonicalCursor && (
              <button
                type="button"
                className="button secondary"
                disabled={loading || loadingMore}
                onClick={() => void loadMoreOperations()}
              >
                {loadingMore ? "Loading older operations…" : "Load older operations"}
              </button>
            )}
            {canonicalAvailable &&
              !canonicalCursor &&
              compareWire(canonicalCount, canonicalTotal) < 0 && (
                <p role="status">
                  The operations API reports additional records but did not provide a continuation
                  cursor.
                </p>
              )}
            {paginationError && <p role="alert">{paginationError}</p>}
          </section>
        )}

        <section className="activity-controls" aria-label="Activity controls">
          <div className="activity-bar">
            <label className="activity-search">
              <span className="sr-only">Search activity</span>
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search activity: action, operator, or technical ID"
              />
            </label>
            <label>
              <span className="sr-only">Area</span>
              <select value={category} onChange={(event) => setCategory(event.target.value)}>
                <option value="">All areas</option>
                {categories.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label>
              <span className="sr-only">Status</span>
              <select
                value={status}
                onChange={(event) => setStatus(event.target.value as ActivityStatus | "")}
              >
                <option value="">All statuses</option>
                <option value="recorded">Recorded</option>
                <option value="in_progress">In progress</option>
                <option value="attention">Needs review</option>
                <option value="unsuccessful">Unsuccessful</option>
                <option value="called_off">Cancelled</option>
                <option value="unknown">Unknown state</option>
              </select>
            </label>
            <button
              type="button"
              className="button secondary"
              aria-expanded={moreOpen}
              aria-controls="activity-more-filters"
              onClick={() => setMoreOpen((open) => !open)}
            >
              More filters{moreActive > 0 ? ` (${moreActive})` : ""}
            </button>
          </div>
          <div className="activity-filters" id="activity-more-filters" hidden={!moreOpen}>
            <label>
              <span>Operator</span>
              <select value={actor} onChange={(event) => setActor(event.target.value)}>
                <option value="">All operators</option>
                {actors.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Operation state</span>
              <select
                value={filters.state ?? ""}
                onChange={(event) =>
                  changeFilters({ ...filters, state: event.target.value || undefined })
                }
              >
                <option value="">All states</option>
                {OPERATION_STATES.map((value) => (
                  <option key={value} value={value}>
                    {titleCase(value)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Spark</span>
              <select
                value={filters.target ?? ""}
                onChange={(event) =>
                  changeFilters({ ...filters, target: event.target.value || undefined })
                }
              >
                <option value="">All Sparks</option>
                {[...targetNames].map(([id, name]) => (
                  <option key={id} value={id}>
                    {name || id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Request ID</span>
              <input
                value={requestText}
                placeholder="Full request ID"
                aria-invalid={requestText !== "" && !REQUEST_ID.test(requestText.trim())}
                onChange={(event) => {
                  const value = event.target.value;
                  setRequestText(value);
                  const id = value.trim().toLowerCase();
                  if (REQUEST_ID.test(id)) changeFilters({ ...filters, requestId: id });
                  else if (!value.trim() && filters.requestId)
                    changeFilters({ ...filters, requestId: undefined });
                }}
              />
            </label>
            <label>
              <span>Sort</span>
              <select
                value={sort}
                onChange={(event) => setSort(event.target.value as "recent" | "attention")}
              >
                <option value="recent">Most recent first</option>
                <option value="attention">Needs attention first</option>
              </select>
            </label>
          </div>
          <div className="activity-control-footer">
            <span role="status">
              {events
                ? `${filtered.length} of ${events.length} loaded ${events.length === 1 ? "event" : "events"} · ${sort === "attention" ? "Needs-review and unsuccessful events first" : "Most recent events first"}`
                : "Loading activity"}
            </span>
            {filtering && filtered.length > 0 && (
              <button type="button" className="activity-clear" onClick={clearFilters}>
                Clear filters
              </button>
            )}
          </div>
        </section>

        {loading && !events && !error && <SkeletonRows columns={4} label="Loading activity" />}
        {error && (
          <section className="activity-state is-error" role="alert">
            <div>
              <strong>Activity unavailable</strong>
              <p>{error}</p>
            </div>
            <button
              type="button"
              className="button secondary"
              disabled={loading || loadingMore}
              onClick={() => setAttempt((value) => value + 1)}
            >
              Retry
            </button>
          </section>
        )}
        {!loading && !error && events && filtered.length === 0 && (
          <EmptyState
            title="No activity yet"
            description="Activity lists the changes made to your fleet, keys and library."
            filtered={filtering}
            onClearFilters={clearFilters}
            action={{ label: "Refresh", onClick: () => setAttempt((value) => value + 1) }}
          />
        )}
        {displayed.length > 0 &&
          (view === "timeline" ? (
            <ActivityTimeline
              api={api}
              events={displayed}
              now={now}
              onJobUpdate={updateOperation}
              onOperationUpdate={updateCanonicalOperation}
              targetNames={targetNames}
            />
          ) : (
            <ActivityTable
              api={api}
              events={displayed}
              now={now}
              onJobUpdate={updateOperation}
              onOperationUpdate={updateCanonicalOperation}
              targetNames={targetNames}
            />
          ))}
      </div>
    </LibraryNodeNamesProvider>
  );
}

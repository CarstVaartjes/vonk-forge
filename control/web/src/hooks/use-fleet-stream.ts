import {LosslessNumber} from "lossless-json";
import {compareWire, type WireNumber} from "../api/contract-numeric";
import {validateComponent} from "../api/contract-json";
import {useCallback, useEffect, useReducer, useRef, useState} from "react";
import type {
  ControlApi,
  FleetChangeEvent,
  FleetRefreshEvent,
  FleetTelemetryEvent,
} from "../api/types";
import {backoffDelay} from "./use-operation-observer";
import {fleetStreamReducer, initialFleetStreamState} from "./fleet-stream-state";

const POLL_INTERVAL_MS = 10_000;
const RECONCILIATION_INTERVAL_MS = 30_000;
const FRESHNESS_TICK_MS = 1_000;
const SPARSE_REFRESH_DELAY_MS = 75;
const SPARSE_RETRY_BASE_MS = 1_000;
const SPARSE_RETRY_MAX_MS = 10_000;
const STALE_AFTER_MS = 90_000;
const MAX_ERROR_LENGTH = 512;

function cursorFrom(event: MessageEvent<string>): WireNumber | null {
  if (!/^[0-9]+$/.test(event.lastEventId)) return null;
  const cursor = Number(event.lastEventId);
  return Number.isSafeInteger(cursor) ? cursor : new LosslessNumber(event.lastEventId);
}

function eventData(event: MessageEvent<string>, component: string): Record<string, unknown> | null {
  try {
    const value: unknown = validateComponent(component, event.data);
    return typeof value === "object" && value !== null ? value as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function errorMessage(value: unknown): string {
  const message = value instanceof Error ? value.message : "Unable to load Fleet";
  return message.length > MAX_ERROR_LENGTH ? `${message.slice(0, MAX_ERROR_LENGTH)}…` : message;
}

export function useFleetStream(api: ControlApi) {
  const [state, dispatch] = useReducer(fleetStreamReducer, initialFleetStreamState);
  const [now, setNow] = useState(() => new Date());
  const [generation, setGeneration] = useState(0);
  // A failed refresh never discards the last snapshot: it is kept, with its age and why it is not fresh.
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number>();
  const [refreshError, setRefreshError] = useState("");
  const retry = useCallback(() => {
    dispatch({type: "retry"});
    setGeneration(value => value + 1);
  }, []);
  const currentRefresh = useRef<((signal?: AbortSignal) => Promise<void>) | undefined>(undefined);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    await currentRefresh.current?.(signal);
  }, []);

  useEffect(() => {
    let active = true;
    let requestInFlight = false;
    let refreshQueued = false;
    let appliedCursor: WireNumber = -1;
    let requiredRefreshCursor: WireNumber | null = null;
    let refreshAttempt = 0;
    let timelineGeneration = 0;
    let pendingReset: string | null = null;
    let sparseRefreshTimer: ReturnType<typeof setTimeout> | undefined;
    let pollingTimer: ReturnType<typeof setInterval> | undefined;
    let reconciliationTimer: ReturnType<typeof setInterval> | undefined;
    const controllers = new Set<AbortController>();
    const freshnessTimer = setInterval(() => setNow(new Date()), FRESHNESS_TICK_MS);

    function scheduleRefresh(delay = SPARSE_REFRESH_DELAY_MS): void {
      if (!active || sparseRefreshTimer !== undefined) return;
      sparseRefreshTimer = setTimeout(() => {
        sparseRefreshTimer = undefined;
        void requestSnapshot("refresh");
      }, delay);
    }

    function scheduleRefreshRetry(): void {
      if (requiredRefreshCursor === null) return;
      const delay = backoffDelay(refreshAttempt, SPARSE_RETRY_BASE_MS, SPARSE_RETRY_MAX_MS);
      refreshAttempt += 1;
      scheduleRefresh(delay);
    }

    async function requestSnapshot(reason: "initial" | "poll" | "reconcile" | "refresh", signal?: AbortSignal): Promise<void> {
      if (!active) return;
      if (requestInFlight) {
        if (reason === "refresh") refreshQueued = true;
        return;
      }
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal?.aborted) return;
      signal?.addEventListener("abort", abort, {once: true});
      const requestTimelineGeneration = timelineGeneration;
      controllers.add(controller);
      requestInFlight = true;
      try {
        const snapshot = await api.visualFleet(controller.signal);
        if (active && !controller.signal.aborted
            && requestTimelineGeneration === timelineGeneration
            && (pendingReset === "cursor-ahead" || compareWire(snapshot.event_cursor, appliedCursor) >= 0)) {
          if (pendingReset !== null) {
            dispatch({type: "reset-snapshot", snapshot, reason: pendingReset});
            pendingReset = null;
          } else dispatch({type: "requested-snapshot", snapshot});
          setLastUpdatedAt(Date.now()); setRefreshError("");
          appliedCursor = snapshot.event_cursor;
          if (requiredRefreshCursor !== null && compareWire(snapshot.event_cursor, requiredRefreshCursor) >= 0) {
            requiredRefreshCursor = null;
            refreshAttempt = 0;
          }
        }
      } catch (value) {
        if (active && !controller.signal.aborted) {
          setRefreshError(errorMessage(value));
          if (requestTimelineGeneration === timelineGeneration && reason === "initial") {
            dispatch({type: "request-error", message: errorMessage(value)});
          }
        }
      } finally {
        signal?.removeEventListener("abort", abort);
        controllers.delete(controller);
        requestInFlight = false;
        if (active && requiredRefreshCursor !== null) {
          const queued = refreshQueued;
          refreshQueued = false;
          if (queued) scheduleRefresh();
          else scheduleRefreshRetry();
        }
      }
    }

    function stopPolling(): void {
      if (pollingTimer === undefined) return;
      clearInterval(pollingTimer);
      pollingTimer = undefined;
    }

    function stopReconciliation(): void {
      if (reconciliationTimer === undefined) return;
      clearInterval(reconciliationTimer);
      reconciliationTimer = undefined;
    }

    function startReconciliation(): void {
      if (reconciliationTimer !== undefined) return;
      reconciliationTimer = setInterval(() => {
        void requestSnapshot("reconcile");
      }, RECONCILIATION_INTERVAL_MS);
    }

    function startPolling(): void {
      if (pollingTimer !== undefined) return;
      pollingTimer = setInterval(() => {
        dispatch({type: "polling-start"});
        void requestSnapshot("poll");
      }, POLL_INTERVAL_MS);
    }

    function onOpen(): void {
      stopPolling();
      startReconciliation();
      dispatch({type: "stream-open"});
    }

    function onError(): void {
      stopReconciliation();
      dispatch({type: "stream-error"});
      startPolling();
    }

    function onRefresh(rawEvent: Event): void {
      const event = rawEvent as MessageEvent<string>;
      const cursor = cursorFrom(event);
      const data = eventData(event, "FleetRefreshEvent") as FleetRefreshEvent | null;
      if (cursor === null || !data || compareWire(data.event_cursor, cursor) !== 0) return;
      // Notices carry no roster authority. Only a completed capture can replace
      // the last model, including a lower cursor after a database timeline reset.
      timelineGeneration += 1;
      pendingReset = data.reset_reason;
      requiredRefreshCursor = cursor;
      refreshAttempt = 0;
      refreshQueued = true;
      dispatch({type: "refresh-notice", cursor, reason: data.reset_reason});
      scheduleRefresh();
    }

    function onTelemetry(rawEvent: Event): void {
      const event = rawEvent as MessageEvent<string>;
      const cursor = cursorFrom(event);
      const data = eventData(event, "FleetTelemetryEvent") as FleetTelemetryEvent | null;
      if (cursor === null || !data
          || typeof data.node_id !== "string"
          || typeof data.sample !== "object" || data.sample === null
          || data.sample.node_id !== data.node_id) return;
      if (requiredRefreshCursor !== null || pendingReset !== null) {
        if (requiredRefreshCursor === null || compareWire(cursor, requiredRefreshCursor) > 0) requiredRefreshCursor = cursor;
        dispatch({type: "projection-refresh", cursor});
        scheduleRefresh();
        return;
      }
      if (compareWire(cursor, appliedCursor) <= 0) return;
      appliedCursor = cursor;
      dispatch({type: "node-telemetry", cursor, nodeId: data.node_id, sample: data.sample, receivedAt: new Date()});
      setLastUpdatedAt(Date.now()); setRefreshError("");
    }

    function onSparse(rawEvent: Event): void {
      const event = rawEvent as MessageEvent<string>;
      const cursor = cursorFrom(event);
      const data = eventData(event, "FleetChangeEvent") as FleetChangeEvent | null;
      if (cursor === null || data?.projection_refresh_required !== true) return;
      if ((pendingReset === null && compareWire(cursor, appliedCursor) <= 0) || compareWire(cursor, requiredRefreshCursor ?? -1) <= 0) return;
      requiredRefreshCursor = cursor;
      dispatch({type: "projection-refresh", cursor});
      scheduleRefresh();
    }

    currentRefresh.current = signal => requestSnapshot("refresh", signal);
    void requestSnapshot("initial");
    const source = typeof EventSource === "function"
      ? new EventSource("/api/fleet/stream")
      : undefined;
    if (source) {
      source.addEventListener("open", onOpen);
      source.addEventListener("error", onError);
      source.addEventListener("fleet-refresh", onRefresh);
      source.addEventListener("node-telemetry", onTelemetry);
      source.addEventListener("node-profile", onSparse);
      source.addEventListener("recipe-state", onSparse);
      source.addEventListener("operation-state", onSparse);
    } else {
      onError();
    }

    return () => {
      active = false;
      currentRefresh.current = undefined;
      stopPolling();
      stopReconciliation();
      clearInterval(freshnessTimer);
      if (sparseRefreshTimer !== undefined) clearTimeout(sparseRefreshTimer);
      source?.removeEventListener("open", onOpen);
      source?.removeEventListener("error", onError);
      source?.removeEventListener("fleet-refresh", onRefresh);
      source?.removeEventListener("node-telemetry", onTelemetry);
      source?.removeEventListener("node-profile", onSparse);
      source?.removeEventListener("recipe-state", onSparse);
      source?.removeEventListener("operation-state", onSparse);
      source?.close();
      for (const controller of controllers) controller.abort();
      controllers.clear();
    };
  }, [api, generation]);

  const stale = Boolean(state.snapshot) && (refreshError !== "" || (lastUpdatedAt !== undefined && now.getTime() - lastUpdatedAt > STALE_AFTER_MS));
  return {...state, now, refresh, retry, lastUpdatedAt, refreshError, stale};
}

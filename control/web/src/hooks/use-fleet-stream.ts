import {useCallback, useEffect, useReducer, useState} from "react";
import type {ControlApi, TelemetryPoint, VisualFleetSnapshot} from "../api/types";
import {fleetStreamReducer, initialFleetStreamState} from "./fleet-stream-state";

const POLL_INTERVAL_MS = 10_000;
const FRESHNESS_TICK_MS = 1_000;
const SPARSE_REFRESH_DELAY_MS = 75;
const MAX_ERROR_LENGTH = 512;

type SnapshotEventData = {
  reset_reason: string;
  schema_version: 1;
  snapshot: VisualFleetSnapshot;
};

type TelemetryEventData = {
  node_id: string;
  sample: TelemetryPoint;
  schema_version: 1;
};

function cursorFrom(event: MessageEvent<string>): number | null {
  if (!/^[0-9]+$/.test(event.lastEventId)) return null;
  const cursor = Number(event.lastEventId);
  return Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : null;
}

function eventData(event: MessageEvent<string>): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(event.data);
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
  const retry = useCallback(() => {
    dispatch({type: "retry"});
    setGeneration(value => value + 1);
  }, []);

  useEffect(() => {
    let active = true;
    let requestInFlight = false;
    let refreshQueued = false;
    let sparseRefreshTimer: ReturnType<typeof setTimeout> | undefined;
    let pollingTimer: ReturnType<typeof setInterval> | undefined;
    const controllers = new Set<AbortController>();
    const freshnessTimer = setInterval(() => setNow(new Date()), FRESHNESS_TICK_MS);

    function scheduleRefresh(): void {
      if (!active || sparseRefreshTimer !== undefined) return;
      sparseRefreshTimer = setTimeout(() => {
        sparseRefreshTimer = undefined;
        void requestSnapshot("refresh");
      }, SPARSE_REFRESH_DELAY_MS);
    }

    async function requestSnapshot(reason: "initial" | "poll" | "refresh"): Promise<void> {
      if (!active) return;
      if (requestInFlight) {
        if (reason === "refresh") refreshQueued = true;
        return;
      }
      const controller = new AbortController();
      controllers.add(controller);
      requestInFlight = true;
      try {
        const snapshot = await api.visualFleet(controller.signal);
        if (active && !controller.signal.aborted) dispatch({type: "requested-snapshot", snapshot});
      } catch (value) {
        if (active && !controller.signal.aborted && reason === "initial") {
          dispatch({type: "request-error", message: errorMessage(value)});
        }
      } finally {
        controllers.delete(controller);
        requestInFlight = false;
        if (active && refreshQueued) {
          refreshQueued = false;
          scheduleRefresh();
        }
      }
    }

    function stopPolling(): void {
      if (pollingTimer === undefined) return;
      clearInterval(pollingTimer);
      pollingTimer = undefined;
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
      dispatch({type: "stream-open"});
    }

    function onError(): void {
      dispatch({type: "stream-error"});
      startPolling();
    }

    function onSnapshot(rawEvent: Event): void {
      const event = rawEvent as MessageEvent<string>;
      const cursor = cursorFrom(event);
      const data = eventData(event) as SnapshotEventData | null;
      if (cursor === null || !data || data.schema_version !== 1
          || typeof data.reset_reason !== "string"
          || typeof data.snapshot !== "object" || data.snapshot === null
          || data.snapshot.event_cursor !== cursor) return;
      dispatch({type: "reset-snapshot", snapshot: data.snapshot, reason: data.reset_reason});
    }

    function onTelemetry(rawEvent: Event): void {
      const event = rawEvent as MessageEvent<string>;
      const cursor = cursorFrom(event);
      const data = eventData(event) as TelemetryEventData | null;
      if (cursor === null || !data || data.schema_version !== 1
          || typeof data.node_id !== "string"
          || typeof data.sample !== "object" || data.sample === null) return;
      dispatch({type: "node-telemetry", cursor, nodeId: data.node_id, sample: data.sample, receivedAt: new Date()});
    }

    function onSparse(rawEvent: Event): void {
      const event = rawEvent as MessageEvent<string>;
      const cursor = cursorFrom(event);
      const data = eventData(event);
      if (cursor === null || data?.schema_version !== 1 || data.projection_refresh_required !== true) return;
      dispatch({type: "projection-refresh", cursor});
      scheduleRefresh();
    }

    void requestSnapshot("initial");
    const source = typeof EventSource === "function"
      ? new EventSource("/api/v1/fleet/stream")
      : undefined;
    if (source) {
      source.addEventListener("open", onOpen);
      source.addEventListener("error", onError);
      source.addEventListener("fleet-snapshot", onSnapshot);
      source.addEventListener("node-telemetry", onTelemetry);
      source.addEventListener("recipe-state", onSparse);
      source.addEventListener("operation-state", onSparse);
    } else {
      onError();
    }

    return () => {
      active = false;
      stopPolling();
      clearInterval(freshnessTimer);
      if (sparseRefreshTimer !== undefined) clearTimeout(sparseRefreshTimer);
      source?.removeEventListener("open", onOpen);
      source?.removeEventListener("error", onError);
      source?.removeEventListener("fleet-snapshot", onSnapshot);
      source?.removeEventListener("node-telemetry", onTelemetry);
      source?.removeEventListener("recipe-state", onSparse);
      source?.removeEventListener("operation-state", onSparse);
      source?.close();
      for (const controller of controllers) controller.abort();
      controllers.clear();
    };
  }, [api, generation]);

  return {...state, now, retry};
}

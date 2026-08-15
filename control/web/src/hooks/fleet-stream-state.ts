import type {TelemetryPoint, VisualFleetSnapshot} from "../api/types";
import {reconcileTelemetryWarnings, telemetryFreshnessAt} from "../lib/fleet";

export type FleetConnectionState = "connecting" | "live" | "reconnecting" | "polling";

export type FleetStreamState = {
  connection: FleetConnectionState;
  error: string;
  lastResetReason: string | null;
  loading: boolean;
  refreshRevision: number;
  requiredRefreshCursor: number | null;
  snapshot?: VisualFleetSnapshot;
};

export const initialFleetStreamState: FleetStreamState = {
  connection: "connecting",
  error: "",
  lastResetReason: null,
  loading: true,
  refreshRevision: 0,
  requiredRefreshCursor: null,
};

export type FleetStreamAction =
  | {type: "requested-snapshot"; snapshot: VisualFleetSnapshot}
  | {type: "reset-snapshot"; snapshot: VisualFleetSnapshot; reason: string}
  | {type: "node-telemetry"; cursor: number; nodeId: string; sample: TelemetryPoint; receivedAt: Date}
  | {type: "projection-refresh"; cursor: number}
  | {type: "stream-open"}
  | {type: "stream-error"}
  | {type: "polling-start"}
  | {type: "request-error"; message: string}
  | {type: "retry"};

function currentCursor(state: FleetStreamState): number {
  return state.snapshot?.event_cursor ?? -1;
}

export function fleetStreamReducer(state: FleetStreamState, action: FleetStreamAction): FleetStreamState {
  switch (action.type) {
    case "requested-snapshot":
      if (action.snapshot.event_cursor < currentCursor(state)) return state;
      return {
        ...state,
        error: "",
        loading: false,
        requiredRefreshCursor: state.requiredRefreshCursor !== null
          && action.snapshot.event_cursor >= state.requiredRefreshCursor
          ? null
          : state.requiredRefreshCursor,
        snapshot: action.snapshot,
      };
    case "reset-snapshot":
      return {
        ...state,
        error: "",
        lastResetReason: action.reason,
        loading: false,
        requiredRefreshCursor: null,
        snapshot: action.snapshot,
      };
    case "node-telemetry": {
      if (!state.snapshot || action.cursor <= state.snapshot.event_cursor || action.sample.node_id !== action.nodeId) return state;
      const freshness = telemetryFreshnessAt(action.sample.observed_at, action.receivedAt);
      const observed = Date.parse(action.sample.observed_at);
      const ageSeconds = Number.isFinite(observed)
        ? Math.max(0, (action.receivedAt.getTime() - observed) / 1000)
        : 0;
      return {
        ...state,
        snapshot: {
          ...state.snapshot,
          event_cursor: action.cursor,
          nodes: state.snapshot.nodes.map(node => node.id === action.nodeId ? {
            ...node,
            telemetry: {age_seconds: ageSeconds, freshness, sample: action.sample},
            warnings: reconcileTelemetryWarnings(node.warnings, freshness),
          } : node),
        },
      };
    }
    case "projection-refresh":
      if (!state.snapshot || action.cursor <= Math.max(state.snapshot.event_cursor, state.requiredRefreshCursor ?? -1)) return state;
      return {
        ...state,
        refreshRevision: state.refreshRevision + 1,
        requiredRefreshCursor: action.cursor,
      };
    case "stream-open":
      return {...state, connection: "live", error: ""};
    case "stream-error":
      return {...state, connection: "reconnecting"};
    case "polling-start":
      return {...state, connection: "polling"};
    case "request-error":
      return {...state, error: action.message, loading: false};
    case "retry":
      return {...state, connection: "connecting", error: "", loading: true};
  }
}

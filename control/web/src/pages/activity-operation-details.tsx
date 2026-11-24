import { formatWire } from "../api/contract-numeric";
import {
  availabilityProgress,
  LibraryAvailabilityProgress,
} from "../components/library/library-availability-progress";
import {
  availabilityFailure,
  LibraryAvailabilityFeedback,
} from "../components/library/library-availability-feedback";
import { useCallback, useEffect, useRef, useState } from "react";
import type { OperationDetail } from "../api/types";
import { operationEnded, stateUpdatesItself } from "./activity-status";
import { type ActivityApi, failureSummary, titleCase } from "./activity-records";
import { DiagnosticDownload } from "./activity-event-parts";

export function ModelCacheCancellationEvidence({ detail }: { detail: OperationDetail }) {
  if (!detail.kind.startsWith("model-cache.")) return null;
  const cancellation = detail.model_cache_cancellation;
  if (!cancellation)
    return detail.state === "cancelled" ? (
      <p style={{ margin: 0 }}>Writer stop evidence unavailable.</p>
    ) : null;
  return (
    <p style={{ margin: 0 }}>
      {cancellation.observation?.detail ??
        (detail.state === "cancelled"
          ? "Cancellation ended; stopping the writer remains unconfirmed."
          : "Cancellation requested; writer stop evidence unavailable.")}
    </p>
  );
}

export function CanonicalOperationDetails({
  api,
  detail,
  onUpdate,
}: {
  api: ActivityApi;
  detail: OperationDetail;
  onUpdate: (detail: OperationDetail) => void;
}) {
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const refresh = useCallback(async () => {
    try {
      const current = await api.operation(detail.id);
      if (mounted.current) {
        onUpdate(current);
        setError("");
      }
    } catch (value) {
      if (mounted.current)
        setError(
          `Current progress could not be loaded. ${value instanceof Error ? value.message : "Try refreshing details."}`,
        );
    }
  }, [api, detail.id, onUpdate]);
  const active = stateUpdatesItself(detail.state);
  useEffect(() => {
    if (!active) return;
    let loading = false;
    const timer = window.setInterval(() => {
      if (loading) return;
      loading = true;
      void refresh().finally(() => {
        loading = false;
      });
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [active, refresh]);
  const availability =
    detail.failure && "code" in detail.failure ? availabilityFailure(detail.failure) : undefined;
  return (
    <div className="activity-job-reason" style={{ gap: ".5rem" }}>
      <ModelCacheCancellationEvidence detail={detail} />
      {availability ? (
        <LibraryAvailabilityFeedback failure={availability} />
      ) : (
        detail.failure && (
          <>
            <strong>{failureSummary(detail.failure)}</strong>
            {"detail" in detail.failure && detail.failure.detail && (
              <p style={{ margin: 0 }}>{detail.failure.detail}</p>
            )}
          </>
        )
      )}
      {detail.evidence_download && <DiagnosticDownload id={detail.id} attempt={detail.attempt} />}
      {detail.progress && (
        <LibraryAvailabilityProgress progress={availabilityProgress(detail.progress)} />
      )}
      {detail.observation_unavailable && <p role="status">Details temporarily unavailable.</p>}
      {detail.projection_issues?.map((issue) => (
        <p key={issue.field} role="status" style={{ margin: 0 }}>
          {titleCase(issue.field)} unavailable: this observation requires{" "}
          {formatWire(issue.observed_bytes)} bytes; its reader budget is{" "}
          {formatWire(issue.budget_bytes)} bytes. Operation identity and state remain known. Refresh
          after the evidence changes.
        </p>
      ))}
      <p className="activity-job-attempt" style={{ margin: 0 }}>
        Attempt {formatWire(detail.attempt)}
        {active ? " · Updates automatically" : ""}
      </p>
      {detail.recovery?.uncertain ||
      (detail.failure && "uncertain" in detail.failure && detail.failure.uncertain) ? (
        <p style={{ margin: 0 }}>
          Outcome uncertain.{" "}
          {detail.recovery?.explanation ?? "Inspect the observed state before recovery."}
        </p>
      ) : (
        detail.recovery?.explanation && <p style={{ margin: 0 }}>{detail.recovery.explanation}</p>
      )}
      {!operationEnded(detail.state) && (!active || error) && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: ".5rem" }}>
          <button type="button" className="button secondary" onClick={() => void refresh()}>
            Refresh operation
          </button>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

import type { JobDetail, OperationDetail } from "../api/types";
import { StatusPill } from "../components/ui/status-pill";
import { type ActivityStatus, activityStatus, statusLabel, statusTone } from "./activity-status";
import {
  type ActivityApi,
  type ActivityRecord,
  activityCategory,
  activityTitle,
} from "./activity-records";
import { EventTime, TargetSummary, TechnicalDetails } from "./activity-event-parts";
import { JobProgressDetails } from "./activity-job-progress";
import { CanonicalOperationDetails } from "./activity-operation-details";

export function ActivityTimeline({
  api,
  events,
  now,
  onJobUpdate,
  onOperationUpdate,
  targetNames,
}: {
  api: ActivityApi;
  onOperationUpdate: (detail: OperationDetail) => void;
  events: ActivityRecord[];
  now: Date;
  onJobUpdate: (detail: JobDetail) => void;
  targetNames: Map<string, string>;
}) {
  return (
    <ol className="activity-timeline" aria-label="Activity timeline">
      {events.map((event, index) => {
        const status = activityStatus(event);
        return (
          <li
            // biome-ignore lint/suspicious/noArrayIndexKey: one request can appear in several events; the position tells them apart
            key={`${event.source}:${event.request_id}:${index}`}
            className={`activity-event is-${status}`}
          >
            <span className="activity-marker" aria-hidden="true" />
            <article>
              <header>
                <div>
                  <h2>{activityTitle(event)}</h2>
                </div>
                <StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill>
              </header>
              <div className="activity-event-meta">
                <span>
                  By <strong>{event.actor || "Unknown operator"}</strong>
                </span>
                <TargetSummary event={event} />
                <EventTime event={event} now={now} />
              </div>
              {event.source === "job" && (
                <JobProgressDetails
                  api={api}
                  event={event}
                  onUpdate={onJobUpdate}
                  targetNames={targetNames}
                />
              )}
              {event.operation && (
                <CanonicalOperationDetails
                  api={api}
                  detail={event.operation}
                  onUpdate={onOperationUpdate}
                />
              )}
              <TechnicalDetails event={event} />
            </article>
          </li>
        );
      })}
    </ol>
  );
}

export function ActivityTable({
  api,
  events,
  now,
  onJobUpdate,
  onOperationUpdate,
  targetNames,
}: {
  api: ActivityApi;
  onOperationUpdate: (detail: OperationDetail) => void;
  events: ActivityRecord[];
  now: Date;
  onJobUpdate: (detail: JobDetail) => void;
  targetNames: Map<string, string>;
}) {
  return (
    <div className="activity-table-wrap">
      <table className="activity-table">
        <caption className="sr-only">Recorded operator and system activity</caption>
        <thead>
          <tr>
            <th scope="col">Event</th>
            <th scope="col">Status</th>
            <th scope="col">Operator</th>
            <th scope="col">When</th>
            <th scope="col">
              <span className="sr-only">Technical details</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {events.map((event, index) => {
            const status = activityStatus(event);
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: one request can appear in several events; the position tells them apart
              <tr key={`${event.source}:${event.request_id}:${index}`}>
                <td data-label="Event">
                  <strong>{activityTitle(event)}</strong>
                  <small>{activityCategory(event)}</small>
                  <TargetSummary compact event={event} />
                </td>
                <td data-label="Status">
                  <StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill>
                </td>
                <td data-label="Operator">{event.actor || "Unknown operator"}</td>
                <td data-label="When">
                  <EventTime event={event} now={now} />
                </td>
                <td className="activity-table-technical">
                  {event.source === "job" && (
                    <JobProgressDetails
                      api={api}
                      event={event}
                      onUpdate={onJobUpdate}
                      targetNames={targetNames}
                    />
                  )}
                  {event.operation && (
                    <CanonicalOperationDetails
                      api={api}
                      detail={event.operation}
                      onUpdate={onOperationUpdate}
                    />
                  )}
                  <TechnicalDetails event={event} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ActivityOverview({
  events,
  filtering,
  loadedCount,
}: {
  events: ActivityRecord[];
  filtering: boolean;
  loadedCount: number;
}) {
  const counts = events.reduce<Record<ActivityStatus, number>>(
    (result, event) => {
      result[activityStatus(event)] += 1;
      return result;
    },
    { recorded: 0, in_progress: 0, attention: 0, unsuccessful: 0, called_off: 0, unknown: 0 },
  );
  const scope = filtering
    ? `${events.length} matching ${events.length === 1 ? "event" : "events"} from ${loadedCount} loaded`
    : `${loadedCount} loaded ${loadedCount === 1 ? "event" : "events"}`;
  return (
    <section
      className="activity-overview"
      aria-label={filtering ? "Matching activity summary" : "Loaded activity summary"}
    >
      <p className="activity-overview-scope">Summary of {scope}.</p>
      <div>
        <span>Recorded</span>
        <strong>{counts.recorded}</strong>
      </div>
      <div>
        <span>In progress</span>
        <strong>{counts.in_progress}</strong>
      </div>
      <div>
        <span>Needs review</span>
        <strong>{counts.attention}</strong>
      </div>
      <div>
        <span>Unsuccessful</span>
        <strong>{counts.unsuccessful}</strong>
      </div>
      <div>
        <span>Cancelled</span>
        <strong>{counts.called_off}</strong>
      </div>
      <div>
        <span>Unknown state</span>
        <strong>{counts.unknown}</strong>
      </div>
    </section>
  );
}

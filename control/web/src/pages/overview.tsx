import "./overview.css";
import { useEffect, useState, type FormEvent, type MouseEvent } from "react";
import type { ControlApi, EnrollmentGrantResponse } from "../api/types";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { useSelectedSpark } from "../hooks/use-selected-spark";
import { type LiveStatus, liveStatus } from "../hooks/fleet-stream-state";
import { fleetStatusLine } from "../lib/fleet";
import { ActivityPreview } from "../components/activity-preview";
import { AttentionItem } from "../components/attention-item";
import { FleetPulse } from "../components/fleet-pulse";
import { FleetFeedback } from "../components/fleet-feedback";
import { WorkloadUnits } from "../components/workload-units";
import { ConfirmDialog } from "../components/confirm-dialog";
import { EmptyState } from "../components/empty-state";
import { PageHeader } from "../components/page-header";
import { useToast } from "../components/toast";
import { EnrollmentGrant } from "../components/enrollment-grant";
import { ServingNow, servingKey } from "../components/serving-now";
import { SparkDetail } from "../components/spark-detail";
import { safeErrorText } from "../lib/error-display";

export function OverviewPage({
  api,
  onLive,
  onNavigate,
}: {
  api: ControlApi;
  onBusyChange?(busy: boolean): void;
  onLive?(status: LiveStatus | null): void;
  onNavigate?(event: MouseEvent<HTMLAnchorElement>, path: string): void;
}) {
  const fleet = useFleetStream(api);
  const live = liveStatus(fleet);
  useEffect(() => onLive?.(live), [onLive, live]);
  useEffect(() => () => onLive?.(null), [onLive]);
  const [enrolling, setEnrolling] = useState(false);
  const [sparkName, setSparkName] = useState("");
  const [grant, setGrant] = useState<EnrollmentGrantResponse | null>(null);
  const toast = useToast();
  const [submitting, setSubmitting] = useState(false);
  const [spark, selectSpark] = useSelectedSpark();
  const [upgrading, setUpgrading] = useState(false);
  const [upgradeBusy, setUpgradeBusy] = useState(false);
  async function upgradeAll() {
    setUpgradeBusy(true);
    try {
      const response = await api.upgradeFleet(true, [], crypto.randomUUID());
      toast.info(
        `Upgrade ${response.state}${response.operation_id ? `. Follow it in Activity (${response.operation_id}).` : "."}`,
      );
    } catch (error) {
      toast.error(
        safeErrorText(error instanceof Error ? error.message : "Could not start the agent upgrade"),
      );
    } finally {
      setUpgradeBusy(false);
      setUpgrading(false);
    }
  }
  async function createGrant(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const name = sparkName.trim();
    if (!name || submitting) return;
    setSubmitting(true);
    try {
      const response = await api.enrollFleetNode({ name, request_key: crypto.randomUUID() });
      if (response.action !== "enroll" || !response.grant)
        throw new Error("Controller did not return an enrollment grant");
      setGrant(response.grant);
      toast.success(`Enrollment grant created for ${name}.`);
    } catch (error) {
      toast.error(
        safeErrorText(
          error instanceof Error ? error.message : "Could not create an enrollment grant",
        ),
      );
    } finally {
      setSubmitting(false);
    }
  }

  function startEnrolling() {
    setEnrolling((value) => !value);
    setGrant(null);
  }

  const nodes = fleet.snapshot?.nodes ?? [];
  return (
    <div className="fleet-page">
      <PageHeader
        title="Overview"
        description="What is serving, and the Sparks that carry it."
        actions={
          <>
            <button type="button" className="button secondary" onClick={fleet.retry}>
              Refresh
            </button>
            <button type="button" className="button secondary" onClick={() => setUpgrading(true)}>
              Upgrade agents
            </button>
            <button type="button" className="button" onClick={startEnrolling}>
              Enroll Spark
            </button>
          </>
        }
      />
      {enrolling && (
        <section className="fleet-enrollment" aria-labelledby="fleet-enrollment-heading">
          <div>
            <h2 id="fleet-enrollment-heading">Enroll a Spark</h2>
            <p>Create a one-use grant that expires in 15 minutes.</p>
          </div>
          {!grant && (
            <form onSubmit={(event) => void createGrant(event)}>
              <label htmlFor="fleet-spark-name">Spark name</label>
              <input
                id="fleet-spark-name"
                required
                maxLength={80}
                value={sparkName}
                onChange={(event) => setSparkName(event.currentTarget.value)}
                placeholder="e.g. Spark at home"
              />
              <button type="submit" className="button" disabled={submitting}>
                {submitting ? "Creating grant…" : "Create enrollment grant"}
              </button>
            </form>
          )}
          {grant && (
            <>
              <EnrollmentGrant api={api} grant={grant} />
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  setGrant(null);
                  setSparkName("");
                }}
              >
                Create another grant
              </button>
            </>
          )}
        </section>
      )}
      {upgrading && (
        <ConfirmDialog
          title="Upgrade every Spark agent?"
          consequence="Each Spark's agent upgrades as soon as that Spark is free, then restarts. Running recipes keep serving unless a Spark reports a problem."
          confirmLabel="Upgrade agents"
          command="vonkctl fleet upgrade --all --yes"
          busy={upgradeBusy}
          onConfirm={() => void upgradeAll()}
          onCancel={() => setUpgrading(false)}
        />
      )}
      <FleetFeedback fleet={fleet} />
      {fleet.snapshot && (
        <p className="fleet-status-line">{fleetStatusLine(fleet.snapshot, fleet.now)}</p>
      )}
      {fleet.snapshot && nodes.length > 0 && <FleetPulse api={api} nodes={nodes} />}
      {fleet.snapshot?.nodes.length === 0 && (
        <EmptyState
          title="No Sparks enrolled"
          description="A Spark is a machine that runs models for your fleet."
          action={{ label: "Enroll a Spark", onClick: () => setEnrolling(true) }}
        />
      )}
      {fleet.snapshot && nodes.length > 0 && (
        <div className="overview-grid">
          <div className="overview-attention">
            {fleet.snapshot.attention_unavailable && (
              <p role="status">Prometheus alerts unavailable. Retrying automatically.</p>
            )}
            {!!fleet.snapshot.attention?.length && (
              <section className="card attention-card" aria-label="Needs attention">
                <h2>Needs attention</h2>
                <ul className="attention-list">
                  {fleet.snapshot.attention.map((alert) => (
                    <AttentionItem key={JSON.stringify(alert.labels)} alert={alert} nodes={nodes} />
                  ))}
                </ul>
              </section>
            )}
          </div>
          <div className="overview-units">
            <WorkloadUnits
              api={api}
              nodes={nodes}
              now={fleet.now}
              live={live === "live"}
              onOpen={selectSpark}
            />
          </div>
          <div className="overview-activity">
            <ActivityPreview
              api={api}
              nodes={nodes}
              now={fleet.now}
              onViewAll={onNavigate && ((event) => onNavigate(event, "/activity"))}
            />
          </div>
          <div className="overview-serving">
            <ServingNow
              api={api}
              refreshKey={servingKey(nodes)}
              pollKey={fleet.snapshot.generated_at}
            />
          </div>
        </div>
      )}
      {spark && <SparkDetail api={api} id={spark} onClose={() => selectSpark(null)} />}
    </div>
  );
}

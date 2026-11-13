import { MetricsChart } from "../components/metrics-chart";
import { useEffect, useState, type FormEvent } from "react";
import type { ControlApi, EnrollmentGrantResponse } from "../api/types";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { type LiveStatus, liveStatus, staleWord } from "../hooks/fleet-stream-state";
import { fleetStatusLine, nodeDisplayName } from "../lib/fleet";
import { SparksTable } from "../components/sparks-table";
import { AttentionItem } from "../components/attention-item";
import { StatusPill } from "../components/status-pill";
import { ConfirmDialog } from "../components/confirm-dialog";
import { EmptyState } from "../components/empty-state";
import { PageHeader } from "../components/page-header";
import { SkeletonRows } from "../components/skeleton";
import { useToast } from "../components/toast";
import { EnrollmentGrant } from "../components/enrollment-grant";
import { ServingNow, servingKey } from "../components/serving-now";
import { SparkDetail } from "../components/spark-detail";
import { ObservationNotice } from "../components/observation-notice";
import { safeErrorText } from "../lib/error-display";

function selectedSpark(): string | null {
  return new URLSearchParams(location.search).get("spark");
}

export function FleetPage({
  api,
  onLive,
}: {
  api: ControlApi;
  onBusyChange?(busy: boolean): void;
  onLive?(status: LiveStatus | null): void;
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
  const [spark, setSpark] = useState(selectedSpark);
  const [upgrading, setUpgrading] = useState(false);
  const [upgradeBusy, setUpgradeBusy] = useState(false);
  useEffect(() => {
    const listener = () => setSpark(selectedSpark());
    addEventListener("popstate", listener);
    return () => removeEventListener("popstate", listener);
  }, []);
  function selectSpark(id: string | null) {
    const url = new URL(location.href);
    if (id) url.searchParams.set("spark", id);
    else url.searchParams.delete("spark");
    history.pushState(null, "", `${url.pathname}${url.search}`);
    setSpark(id);
  }
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

  return (
    <div className="fleet-page">
      <PageHeader
        title="Fleet"
        description="Every enrolled Spark, its health, and what is running there."
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
      {fleet.snapshot?.attention_unavailable && (
        <p role="status">Prometheus alerts unavailable. Retrying automatically.</p>
      )}
      {!!fleet.snapshot?.attention?.length && (
        <section aria-label="Needs attention">
          <h2>Needs attention</h2>
          <ul className="attention-list">
            {fleet.snapshot.attention.map((alert) => (
              <AttentionItem
                key={JSON.stringify(alert.labels)}
                alert={alert}
                nodes={fleet.snapshot?.nodes ?? []}
              />
            ))}
          </ul>
        </section>
      )}
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
      {fleet.snapshot && fleet.stale && (
        <div className="fleet-stale">
          <StatusPill tone="warn">{staleWord(fleet.refreshError)}</StatusPill>
          <ObservationNotice
            connection="reconnecting"
            lastSuccessAt={fleet.lastUpdatedAt}
            subject="the Fleet"
          />
        </div>
      )}
      {fleet.snapshot && (
        <ServingNow
          api={api}
          refreshKey={servingKey(fleet.snapshot.nodes)}
          pollKey={fleet.snapshot.generated_at}
        />
      )}
      {fleet.snapshot && (
        <p className="fleet-status-line">{fleetStatusLine(fleet.snapshot, fleet.now)}</p>
      )}
      {fleet.snapshot && fleet.snapshot.nodes.length > 0 && (
        <SparksTable
          nodes={fleet.snapshot.nodes}
          now={fleet.now}
          selected={spark}
          onSelect={selectSpark}
        />
      )}
      {fleet.loading && !fleet.snapshot && !fleet.error && (
        <SkeletonRows columns={6} label="Loading Fleet" />
      )}
      {fleet.error && !fleet.snapshot && (
        <section className="fleet-error" role="alert">
          <h2>Fleet unavailable</h2>
          <p>{fleet.error}</p>
          <button type="button" className="button" onClick={fleet.retry}>
            Retry
          </button>
        </section>
      )}
      {fleet.snapshot?.nodes.length === 0 && (
        <EmptyState
          title="No Sparks enrolled"
          description="A Spark is a machine that runs models for your fleet."
          action={{ label: "Enroll a Spark", onClick: () => setEnrolling(true) }}
        />
      )}
      <MetricsChart
        api={api}
        names={Object.fromEntries(
          (fleet.snapshot?.nodes ?? []).map((node) => [node.id, nodeDisplayName(node)]),
        )}
      />
      {spark && <SparkDetail api={api} id={spark} onClose={() => selectSpark(null)} />}
    </div>
  );
}

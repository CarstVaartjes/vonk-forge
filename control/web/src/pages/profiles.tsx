import {useState} from "react";
import type {ControlApi, FleetEvidenceResponse, ReconciliationPlan as Plan} from "../api/types";
import {ReconciliationPlan} from "../components/reconciliation-plan";
import {RepositoryEditor} from "../components/repository-editor";

const MAX_PROFILE_ID_LENGTH = 64;
const MAX_ERROR_LENGTH = 512;

function boundedError(value: unknown): string {
  const message = value instanceof Error ? value.message : "Unable to preview reconciliation plan";
  return message.length > MAX_ERROR_LENGTH ? `${message.slice(0, MAX_ERROR_LENGTH)}…` : message;
}

export function ProfilesPage({api}: {api: ControlApi}) {
  const [profileId, setProfileId] = useState("");
  const [plan, setPlan] = useState<Plan>();
  const [fleet, setFleet] = useState<FleetEvidenceResponse>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function previewPlan(event: React.FormEvent) {
    event.preventDefault();
    setPlan(undefined);
    setFleet(undefined);
    setError("");
    if (!/^[a-z0-9][a-z0-9-]{1,63}$/.test(profileId)) {
      setError("Provide a valid repository profile ID.");
      return;
    }
    setLoading(true);
    try {
      const [planned, liveFleet] = await Promise.all([
        api.planProfile(profileId),
        api.fleetEvidence(),
      ]);
      setPlan(planned);
      setFleet(liveFleet);
    } catch (value) {
      setError(boundedError(value));
    } finally {
      setLoading(false);
    }
  }

  return <>
    <RepositoryEditor api={api} kind="profiles"/>
    <section className="reconciliation-workflow" aria-labelledby="reconcile-profile-heading">
      <h3 id="reconcile-profile-heading">Reconcile a repository profile</h3>
      <p>Preview the canonical server plan, verify live agent gates, then apply only its exact digest.</p>
      <form onSubmit={event => void previewPlan(event)}>
        <label>Profile ID to reconcile
          <input disabled={loading} maxLength={MAX_PROFILE_ID_LENGTH} required value={profileId} onChange={event => setProfileId(event.target.value)}/>
        </label>
        <button type="submit" disabled={loading}>{loading ? "Loading exact plan…" : "Preview exact plan"}</button>
      </form>
      {loading && <p role="status">Loading the server-issued plan and live acceptance gates…</p>}
      {error && <p role="alert">{error}</p>}
      {plan && fleet && <ReconciliationPlan key={plan.digest} api={api} fleet={fleet} plan={plan}/>}
    </section>
  </>;
}

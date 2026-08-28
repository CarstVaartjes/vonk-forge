import {useEffect, useMemo, useState} from "react";
import type {
  ControlApi,
  FleetProfile,
  FleetProfileApplication,
  FleetProfilePreview,
  VisualFleetNode,
} from "../api/types";
import {nodeDisplayName, nodeOperationalState} from "../lib/fleet";

type MatrixState = "running" | "installed" | "degraded" | "planned" | "empty";
type MatrixCell = {detail: string; state: MatrixState};
type WorkloadRow = {
  cells: Map<string, MatrixCell>;
  key: string;
  model?: string | null;
  title: string;
};

const TERMINAL_APPLICATION_STATES = new Set(["succeeded", "failed", "cancelled", "waiting-for-operator"]);

function stateLabel(state: MatrixState): string {
  if (state === "planned") return "Profile change";
  return state.charAt(0).toUpperCase() + state.slice(1);
}

function profileStatus(preview?: FleetProfilePreview): {label: string; tone: "good" | "attention" | "danger" | "neutral"} {
  if (!preview) return {label: "Checking live state", tone: "neutral"};
  if (!preview.allowed) return {label: `${preview.summary.blockers} blocked`, tone: "danger"};
  if (preview.steps.length === 0) return {label: "Live state matches", tone: "good"};
  return {label: `${preview.steps.length} changes`, tone: "attention"};
}

function workloadRows(nodes: readonly VisualFleetNode[], profile?: FleetProfile): WorkloadRow[] {
  const rows = new Map<string, WorkloadRow>();
  const ensure = (key: string, title: string, model?: string | null) => {
    const current = rows.get(key);
    if (current) {
      if (!current.model && model) current.model = model;
      return current;
    }
    const row: WorkloadRow = {cells: new Map<string, MatrixCell>(), key, model, title};
    rows.set(key, row);
    return row;
  };

  for (const node of nodes) {
    for (const installed of node.installed) {
      const row = ensure(installed.recipe_revision_id, installed.title);
      row.cells.set(node.id, {
        detail: installed.complete ? installed.topology_name : installed.degraded_reason ?? installed.group_state,
        state: installed.complete ? "installed" : "degraded",
      });
    }
    for (const loaded of node.loaded) {
      const row = ensure(loaded.recipe_revision_id, loaded.alias);
      row.cells.set(node.id, {
        detail: loaded.healthy ? loaded.alias : loaded.degraded_reason ?? loaded.group_state,
        state: loaded.healthy ? "running" : "degraded",
      });
    }
  }

  for (const assignment of profile?.assignments ?? []) {
    const row = ensure(assignment.recipe_revision_id, assignment.recipe_title, assignment.model_title);
    for (const member of assignment.nodes) {
      if (row.cells.has(member.node_id)) continue;
      row.cells.set(member.node_id, {
        detail: assignment.desired_state === "running" ? assignment.alias ?? "Start workload" : "Install recipe",
        state: "planned",
      });
    }
  }

  return [...rows.values()].sort((left, right) => {
    const stateRank = (row: WorkloadRow) => Math.max(...[...row.cells.values()].map(cell => cell.state === "degraded" ? 3 : cell.state === "planned" ? 2 : cell.state === "running" ? 1 : 0));
    return stateRank(right) - stateRank(left) || left.title.localeCompare(right.title);
  });
}

function ApplicationProgress({application}: {application: FleetProfileApplication}) {
  const percentage = application.total_steps === 0 ? 100 : Math.round(application.current_step / application.total_steps * 100);
  return <section className={`profile-application profile-application-${application.state}`} aria-live="polite">
    <div>
      <strong>{application.state === "succeeded" ? "Profile applied" : application.state === "running" || application.state === "queued" ? "Applying profile" : "Profile needs attention"}</strong>
      <span>{application.current_step} of {application.total_steps} steps · {application.state.replaceAll("-", " ")}</span>
    </div>
    <div className="profile-application-track" role="progressbar" aria-label="Fleet Profile application progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentage}><span style={{transform: `scaleX(${percentage / 100})`}}/></div>
    {application.status_reason && <p>{application.status_reason}</p>}
  </section>;
}

export function FleetOperatingBoard({api, nodes, now, onManageNode}: {
  api: ControlApi;
  nodes: readonly VisualFleetNode[];
  now: Date;
  onManageNode(nodeId: string): void;
}) {
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>();
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [application, setApplication] = useState<FleetProfileApplication>();
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const nodeStateKey = useMemo(() => nodes.map(node => [
    node.id,
    node.connection.online_state,
    node.installed.map(item => `${item.recipe_revision_id}:${item.rank_state}:${item.group_state}`).sort().join(","),
    node.loaded.map(item => `${item.recipe_revision_id}:${item.rank_state}:${item.run_state}:${item.route_state}`).sort().join(","),
    node.warnings.map(item => `${item.code}:${item.severity}`).sort().join(","),
  ].join("~")).sort().join("|"), [nodes]);

  useEffect(() => {
    if (typeof api.fleetProfiles !== "function") {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api.fleetProfiles(controller.signal).then(result => {
      setProfiles(result.profiles);
      setSelectedProfileId(current => current && result.profiles.some(profile => profile.id === current)
        ? current
        : result.profiles[0]?.id);
      setError("");
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Fleet Profiles are unavailable.");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [api]);

  useEffect(() => {
    if (!selectedProfileId) {
      setPreview(undefined);
      return;
    }
    const controller = new AbortController();
    setPreviewing(true);
    api.previewFleetProfile(selectedProfileId, controller.signal).then(result => {
      setPreview(result);
      setError("");
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "The profile preview is unavailable.");
    }).finally(() => {
      if (!controller.signal.aborted) setPreviewing(false);
    });
    return () => controller.abort();
  }, [api, nodeStateKey, previewAttempt, selectedProfileId]);

  useEffect(() => {
    if (!application || TERMINAL_APPLICATION_STATES.has(application.state)) return;
    const controller = new AbortController();
    const poll = window.setInterval(() => {
      api.fleetProfileApplication(application.id, controller.signal).then(next => {
        setApplication(next);
        if (TERMINAL_APPLICATION_STATES.has(next.state)) {
          window.clearInterval(poll);
          setPreviewAttempt(value => value + 1);
        }
      }).catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Profile progress is unavailable.");
      });
    }, 1_000);
    return () => {
      controller.abort();
      window.clearInterval(poll);
    };
  }, [api, application]);

  const selectedProfile = profiles.find(profile => profile.id === selectedProfileId);
  const rows = useMemo(() => workloadRows(nodes, selectedProfile), [nodes, selectedProfile]);
  const status = profileStatus(preview);
  const liveCount = nodes.filter(node => nodeOperationalState(node, now) === "live").length;
  const exceptionCount = nodes.filter(node => nodeOperationalState(node, now) !== "live" || node.warnings.length > 0).length;

  async function applyProfile() {
    if (!selectedProfile || !preview?.allowed || preview.steps.length === 0 || applying) return;
    setApplying(true);
    setError("");
    try {
      const next = await api.applyFleetProfile(selectedProfile.id, preview.plan_digest);
      setApplication(next);
      if (TERMINAL_APPLICATION_STATES.has(next.state)) setPreviewAttempt(value => value + 1);
    } catch (value) {
      setError(value instanceof Error ? value.message : "The Fleet Profile could not be applied.");
    } finally {
      setApplying(false);
    }
  }

  return <section className="fleet-operating-board" aria-labelledby="workload-map-title">
    <header className="fleet-state-strip">
      <div className="fleet-profile-current">
        <span>Selected profile</span>
        <strong>{selectedProfile?.name ?? (loading ? "Loading profiles…" : "Live fleet")}</strong>
      </div>
      <dl className="fleet-state-facts">
        <div><dt>Sparks ready</dt><dd>{liveCount} / {nodes.length}</dd></div>
        <div><dt>Running</dt><dd>{nodes.reduce((count, node) => count + node.loaded.filter(item => item.healthy).length, 0)}</dd></div>
        <div><dt>Installed</dt><dd>{nodes.reduce((count, node) => count + node.installed.filter(item => item.complete).length, 0)}</dd></div>
        <div className={exceptionCount ? "has-exceptions" : ""}><dt>Attention</dt><dd>{exceptionCount}</dd></div>
      </dl>
      <div className="fleet-profile-actions">
        <span className={`profile-match profile-match-${status.tone}`}>{previewing ? "Checking live state" : status.label}</span>
        <button type="button" disabled={!preview?.allowed || preview.steps.length === 0 || applying} onClick={() => void applyProfile()}>{applying ? "Starting…" : preview && !preview.allowed ? "Resolve blockers" : preview?.steps.length ? `Apply ${preview.steps.length} changes` : "Profile applied"}</button>
      </div>
    </header>

    {error && <div className="operating-board-error" role="alert"><strong>Operating plan unavailable</strong><span>{error}</span></div>}
    {application && <ApplicationProgress application={application}/>}

    <div className="fleet-operating-layout">
      <aside className="fleet-profile-rail" aria-label="Fleet Profiles">
        <div className="profile-rail-heading"><strong>Fleet profiles</strong><span>{profiles.length}</span></div>
        {loading && <p role="status">Loading saved profiles…</p>}
        {!loading && profiles.length === 0 && <div className="profile-rail-empty"><strong>No saved profiles</strong><p>Choose a recipe in Library to create the first repeatable Fleet setup.</p><a className="button secondary" href="/library?profile=new">Browse Library</a></div>}
        {profiles.map(profile => <button key={profile.id} type="button" className="profile-rail-item" aria-pressed={profile.id === selectedProfileId} onClick={() => { setApplication(undefined); setSelectedProfileId(profile.id); }}>
          <span>{profile.name}</span>
          <small>{profile.assignments.length} {profile.assignments.length === 1 ? "workload" : "workloads"} · {profile.installation_policy === "exact" ? "Exact" : "Keep cached"}</small>
        </button>)}
        <a className="profile-create-link" href="/library?profile=new">+ Build a profile</a>
      </aside>

      <div className="workload-map-wrap">
        <div className="workload-map-heading">
          <div><h2 id="workload-map-title">Workload map</h2><p>Live models and recipes across every Spark, compared with the selected profile.</p></div>
          <div className="workload-legend" aria-label="Workload states"><span className="state-running">Running</span><span className="state-installed">Installed</span><span className="state-planned">Profile change</span><span className="state-degraded">Attention</span></div>
        </div>
        <div className="workload-matrix-scroll">
          <table className="workload-matrix">
            <thead><tr><th scope="col">Model / recipe</th>{nodes.map(node => {
              const state = nodeOperationalState(node, now);
              return <th key={node.id} scope="col"><button type="button" aria-label={`Manage ${nodeDisplayName(node)} — ${state}`} onClick={() => onManageNode(node.id)}><span>{nodeDisplayName(node)}</span><small>{state}</small></button></th>;
            })}</tr></thead>
            <tbody>
              {rows.map(row => <tr key={row.key}><th scope="row"><strong>{row.model ?? row.title}</strong>{row.model && <small>{row.title}</small>}</th>{nodes.map(node => {
                const cell = row.cells.get(node.id) ?? {detail: "Not present", state: "empty" as const};
                return <td key={node.id} className={`matrix-cell matrix-${cell.state}`}><span>{stateLabel(cell.state)}</span><small>{cell.detail}</small></td>;
              })}</tr>)}
              {rows.length === 0 && <tr><td className="workload-matrix-empty" colSpan={nodes.length + 1}>No installed or running workloads yet. Open Library to choose a model and recipe.</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="workload-stack" role="list" aria-label="Workloads by Spark">
          {rows.map(row => <article key={row.key} className="workload-stack-row" role="listitem">
            <header><strong>{row.model ?? row.title}</strong>{row.model && <small>{row.title}</small>}</header>
            <ul>{nodes.map(node => {
              const cell = row.cells.get(node.id) ?? {detail: "Not present", state: "empty" as const};
              const state = nodeOperationalState(node, now);
              return <li key={node.id} className={`workload-stack-cell matrix-${cell.state}`}>
                <button type="button" aria-label={`Manage ${nodeDisplayName(node)} from mobile workload map — ${state}`} onClick={() => onManageNode(node.id)}><span>{nodeDisplayName(node)}</span><small>{state}</small></button>
                <div><strong>{stateLabel(cell.state)}</strong><small>{cell.detail}</small></div>
              </li>;
            })}</ul>
          </article>)}
          {rows.length === 0 && <p className="workload-stack-empty">No installed or running workloads yet. Open Library to choose a model and recipe.</p>}
        </div>
      </div>

      {selectedProfile && preview && <aside className="profile-plan" aria-label="Profile change preview">
        <div><strong>{!preview.allowed ? "Blocked" : preview.steps.length ? `${preview.steps.length} changes` : "In sync"}</strong><span>{selectedProfile.description || "Saved Fleet operating state"}</span></div>
        {preview.reasons.length > 0 && <ul>{preview.reasons.slice(0, 4).map(reason => <li key={`${reason.code}:${reason.detail}`} className={`reason-${reason.severity}`}><strong>{reason.code.split(".").at(-1)?.replaceAll("_", " ")}</strong><span>{reason.detail}</span></li>)}</ul>}
        {preview.steps.length > 0 && <ol>{preview.steps.slice(0, 6).map(step => {
          const nodeCount = step.node_ids?.length ?? 0;
          return <li key={step.index}><span>{step.label}</span><small>{nodeCount} {nodeCount === 1 ? "Spark" : "Sparks"}</small></li>;
        })}</ol>}
        {preview.steps.length > 6 && <p>+ {preview.steps.length - 6} more planned changes</p>}
      </aside>}
    </div>
  </section>;
}

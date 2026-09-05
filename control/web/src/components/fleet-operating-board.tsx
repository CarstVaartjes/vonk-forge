import {useEffect, useState} from "react";
import type {ControlApi, FleetProfile, FleetProfileApplication, FleetProfilePreview} from "../api/types";

const TERMINAL_APPLICATION_STATES = new Set(["succeeded", "failed", "cancelled"]);

function profileStatus(preview?: FleetProfilePreview): {label: string; tone: "good" | "attention" | "danger" | "neutral"} {
  if (!preview) return {label: "Checking current setup", tone: "neutral"};
  if (!preview.allowed) return {label: "Needs attention", tone: "danger"};
  if (preview.steps.length === 0) return {label: "Up to date", tone: "good"};
  return {label: `${preview.steps.length} changes ready`, tone: "attention"};
}

function progressRecord(application: FleetProfileApplication): Record<string, unknown> {
  return application.progress && typeof application.progress === "object" ? application.progress : {};
}

function bytes(value: unknown): string | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return undefined;
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = "B";
  for (const next of units) {
    amount /= 1024;
    unit = next;
    if (amount < 1024) break;
  }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${unit}`;
}

function applicationPhase(application: FleetProfileApplication): string {
  const record = progressRecord(application);
  for (const key of ["message", "phase", "current_phase", "current_step_label"]) {
    if (typeof record[key] === "string" && record[key]) return record[key] as string;
  }
  return application.status_reason || (application.state === "succeeded" ? "Profile is running" : "Switch in progress");
}

function memberProgress(application: FleetProfileApplication): Array<{completed?: string; nodeId: string; state: string; total?: string}> {
  const members = progressRecord(application).members;
  if (!Array.isArray(members)) return [];
  return members.flatMap(member => {
    if (!member || typeof member !== "object") return [];
    const record = member as Record<string, unknown>;
    const nodeId = typeof record.node_id === "string" ? record.node_id : undefined;
    if (!nodeId) return [];
    const state = typeof record.state === "string" ? record.state : "pending";
    return [{nodeId, state, completed: bytes(record.completed_bytes), total: bytes(record.total_bytes)}];
  });
}

function ApplicationProgress({application}: {application: FleetProfileApplication}) {
  const progress = progressRecord(application);
  const completed = bytes(progress.completed_bytes);
  const total = bytes(progress.total_bytes);
  const members = memberProgress(application);
  const hasTotal = typeof progress.total_bytes === "number" && Number.isFinite(progress.total_bytes);
  const value = hasTotal && typeof progress.completed_bytes === "number"
    ? Math.min(100, Math.max(0, progress.completed_bytes / Number(progress.total_bytes) * 100))
    : undefined;
  return <section className={`fleet-profile-progress state-${application.state}`} aria-live="polite" aria-label="Profile switch progress">
    <div className="fleet-profile-progress-heading"><div><strong>{applicationPhase(application)}</strong><span>{application.state.replaceAll("-", " ")}</span></div>{completed && <span>{completed}{total ? ` of ${total}` : ""}</span>}</div>
    <div className={`fleet-profile-progress-track${value === undefined ? " is-indeterminate" : ""}`} role="progressbar" aria-label="Profile switch progress" aria-valuemin={0} aria-valuemax={100} {...(value === undefined ? {"aria-valuetext": "Progress total unavailable"} : {"aria-valuenow": value})}><span style={value === undefined ? undefined : {transform: `scaleX(${value / 100})`}}/></div>
    {members.length > 0 && <ul className="fleet-profile-progress-members" aria-label="Profile switch targets">{members.map(member => <li key={member.nodeId}><span>{member.nodeId}</span><small>{member.state}{member.completed ? ` · ${member.completed}${member.total ? ` of ${member.total}` : ""}` : ""}</small></li>)}</ul>}
    {application.status_reason && <p>{application.status_reason}</p>}
  </section>;
}

/** The Fleet page keeps only a compact shortcut; the full editor lives in Library. */
export function FleetOperatingBoard({api}: {api: ControlApi; nodes?: unknown; now?: Date; onManageNode?(nodeId: string): void}) {
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>();
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [application, setApplication] = useState<FleetProfileApplication>();
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [requestKey, setRequestKey] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    api.fleetProfiles(controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setProfiles(result.profiles);
      setSelectedProfileId(current => current && result.profiles.some(profile => profile.id === current) ? current : result.profiles[0]?.id);
      setError("");
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Saved profiles are unavailable.");
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
      if (!controller.signal.aborted) {
        setPreview(result);
        setError("");
      }
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "The current profile status is unavailable.");
    }).finally(() => {
      if (!controller.signal.aborted) setPreviewing(false);
    });
    return () => controller.abort();
  }, [api, attempt, selectedProfileId]);

  useEffect(() => {
    if (!application || TERMINAL_APPLICATION_STATES.has(application.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api.fleetProfileApplication(application.id, controller.signal).then(next => {
        if (!controller.signal.aborted) setApplication(next);
      }).catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Switch progress is unavailable.");
      });
    }, 1_000);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [api, application]);

  const selectedProfile = profiles.find(profile => profile.id === selectedProfileId);
  const status = profileStatus(preview);

  async function switchProfile() {
    if (!selectedProfile || !preview?.allowed || preview.steps.length === 0 || applying) return;
    setApplying(true);
    setError("");
    try {
      const key = requestKey ?? crypto.randomUUID();
      setRequestKey(key);
      const next = await api.applyFleetProfile(selectedProfile.id, {plan_digest: preview.plan_digest, request_key: key});
      setApplication(next);
      if (next.state === "succeeded") setRequestKey(undefined);
      if (TERMINAL_APPLICATION_STATES.has(next.state)) setAttempt(value => value + 1);
    } catch (value) {
      setError(value instanceof Error ? value.message : "The profile switch could not be started.");
    } finally {
      setApplying(false);
    }
  }

  return <section className="fleet-profile-shortcut" aria-labelledby="fleet-profile-shortcut-title">
    <div className="fleet-profile-shortcut-copy"><span className="fleet-section-label">Active profile</span><strong id="fleet-profile-shortcut-title">{selectedProfile?.name ?? (loading ? "Loading saved profiles…" : "No active profile")}</strong>{selectedProfile?.description && <p>{selectedProfile.description}</p>}</div>
    <div className="fleet-profile-shortcut-actions"><span className={`profile-match profile-match-${status.tone}`} role="status">{previewing ? "Checking current setup" : status.label}</span><button type="button" className="button" disabled={applying || !preview?.allowed || !preview.steps.length} onClick={() => void switchProfile()}>{applying ? "Switching…" : "Switch profile"}</button><a className="button secondary" href="/library/profiles">Manage profiles</a></div>
    {preview && !preview.allowed && preview.reasons[0] && <p className="fleet-profile-shortcut-reason"><strong>Why it is paused</strong> {preview.reasons[0].detail}</p>}
    {error && <div className="fleet-profile-shortcut-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>Retry</button></div>}
    {application && <ApplicationProgress application={application}/>}
  </section>;
}

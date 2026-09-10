import {useEffect, useState} from "react";
import type {ControlApi, FleetProfile, FleetProfileApplicationView, FleetProfilePreview} from "../api/types";
import {availabilityProgress, LibraryAvailabilityProgress} from "./library-availability-progress";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

function progressRecord(application: FleetProfileApplicationView): Record<string, unknown> {
  const progress = application.progress as Record<string, unknown>;
  const child = progress.child_progress;
  return child && typeof child === "object" ? child as Record<string, unknown> : progress;
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

function ApplicationProgress({application}: {application: FleetProfileApplicationView}) {
  const progress = progressRecord(application);
  const completed = bytes(progress.bytes);
  const total = bytes(progress.total_bytes);
  const nodeIds = Array.isArray(progress.node_ids) ? progress.node_ids.filter((id): id is string => typeof id === "string") : [];
  const phase = typeof progress.phase === "string" ? progress.phase.replaceAll("-", " ") : "Profile load";
  return <section className={`fleet-profile-progress state-${application.state}`} aria-live="polite" aria-label="Profile load progress">
    <div className="fleet-profile-progress-heading"><div><strong>{phase}</strong><span>{application.state.replaceAll("-", " ")}</span></div>{completed && <span>{completed}{total ? ` of ${total}` : ""}</span>}</div>
    <LibraryAvailabilityProgress progress={availabilityProgress(progress)}/>
    {nodeIds.length > 0 && <ul className="fleet-profile-progress-members" aria-label="Profile load targets">{nodeIds.map(nodeId => <li key={nodeId}><span>{nodeId}</span><small>Participating</small></li>)}</ul>}
    {application.status_reason && <p>{application.status_reason}</p>}
  </section>;
}

/** The Fleet page keeps only a compact shortcut; the full editor lives in Library. */
export function FleetOperatingBoard({api}: {api: ControlApi; nodes?: unknown; now?: Date; onManageNode?(nodeId: string): void}) {
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [selectedNumber, setSelectedNumber] = useState<number>();
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [application, setApplication] = useState<FleetProfileApplicationView>();
  const [loading, setLoading] = useState(true);
  const [previewing, setPreviewing] = useState(false);
  const [loadingProfile, setLoadingProfile] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void api.profiles(controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setProfiles(result.profiles);
      setSelectedNumber(current => current && result.profiles.some(profile => profile.number === current) ? current : result.profiles[0]?.number);
      setError("");
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Saved profiles are unavailable.");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api]);

  useEffect(() => {
    if (selectedNumber === undefined) { setPreview(undefined); return; }
    const controller = new AbortController();
    setPreviewing(true);
    void api.previewProfile(selectedNumber, controller.signal).then(result => {
      if (!controller.signal.aborted) { setPreview(result); setError(""); }
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "The current profile status is unavailable.");
    }).finally(() => { if (!controller.signal.aborted) setPreviewing(false); });
    return () => controller.abort();
  }, [api, selectedNumber]);

  useEffect(() => {
    if (selectedNumber === undefined || !application || TERMINAL_STATES.has(application.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api.profileProgress(selectedNumber, controller.signal).then(next => {
        if (!controller.signal.aborted) setApplication(next);
      }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Profile load progress is unavailable."); });
    }, 1_000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, application, selectedNumber]);

  const selectedProfile = profiles.find(profile => profile.number === selectedNumber);

  async function load() {
    if (selectedNumber === undefined || !preview?.allowed || loadingProfile) return;
    setLoadingProfile(true); setError("");
    try {
      setApplication(await api.loadProfile(selectedNumber, {dry_run: false, request_key: crypto.randomUUID()}));
    } catch (value) {
      setError(value instanceof Error ? value.message : "The profile could not be loaded.");
    } finally { setLoadingProfile(false); }
  }

  return <section className="fleet-profile-shortcut" aria-labelledby="fleet-profile-shortcut-title">
    <div className="fleet-profile-shortcut-copy"><span className="fleet-section-label">Active profile</span><strong id="fleet-profile-shortcut-title">{selectedProfile ? `Profile ${selectedProfile.number} · ${selectedProfile.name}` : loading ? "Loading saved profiles…" : "No saved profile"}</strong>{selectedProfile?.description && <p>{selectedProfile.description}</p>}</div>
    <div className="fleet-profile-shortcut-actions"><span className="profile-match profile-match-neutral" role="status">{previewing ? "Checking current setup" : preview ? (preview.allowed ? (preview.steps.length ? "Ready to load" : "Already loaded") : "Needs attention") : "Profile unavailable"}</span><button type="button" className="button" disabled={loadingProfile || !preview?.allowed} onClick={() => void load()}>{loadingProfile ? "Loading…" : "Load profile"}</button><a className="button secondary" href="/library/profiles">Manage profiles</a></div>
    {preview && !preview.allowed && preview.reasons[0] && <p className="fleet-profile-shortcut-reason"><strong>Why it is paused</strong> {preview.reasons[0].detail}</p>}
    {error && <div className="fleet-profile-shortcut-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => setError("")}>Dismiss</button></div>}
    {application && <ApplicationProgress application={application}/>}
  </section>;
}

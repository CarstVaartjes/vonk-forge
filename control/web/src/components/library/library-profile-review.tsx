import { compareWire, formatWire } from "../../api/contract-numeric";
import { ProfileLoadAction } from "../../api/vocabulary.generated";
import type { FleetProfilePreview } from "../../api/types";

const SUMMARY_LABELS: [keyof FleetProfilePreview["summary"], string][] = [
  ["starts", "start"],
  ["stops", "stop"],
  ["installs", "install"],
  ["uninstalls", "uninstall"],
  ["builds", "build"],
  ["distributions", "distribute"],
  ["placements", "place"],
];

/** Review changes: what applying will do, blockers first; selectors and digests stay in the detail. */
export function ProfileReview({ preview }: { preview: FleetProfilePreview }) {
  const counts = SUMMARY_LABELS.filter(
    ([key]) => compareWire(preview.summary?.[key] ?? 0, 0) > 0,
  ).map(([key, label]) => `${formatWire(preview.summary[key])} ${label}`);
  return (
    <section className="library-profile-review" aria-label="Review changes">
      <h4>Review changes</h4>
      {preview.waits_for_preparation && !preview.allowed && (
        <p className="library-profile-plain-note">
          {[
            "The Controller prepares this first, then loads:",
            ...(preview.preparation_steps ?? []).map((step) => step.label),
          ].join(" · ")}
        </p>
      )}
      {preview.next_action === ProfileLoadAction.BLOCKED && preview.reasons[0] && (
        <p className="library-profile-plain-error" role="alert">
          {preview.reasons[0].detail}
        </p>
      )}
      {(preview.reasons ?? [])
        .slice(preview.next_action === ProfileLoadAction.BLOCKED ? 1 : 0)
        .map((reason) => (
          <p key={`${reason.code}:${reason.detail}`} className="library-profile-plain-note">
            {reason.detail}
          </p>
        ))}
      {(preview.effects.gpu_clocks ?? []).map((effect) => (
        <p key={effect.node_id}>
          GPU range on {effect.node_id}: {formatWire(effect.desired.min_mhz)}–
          {formatWire(effect.desired.max_mhz)} MHz
          {effect.previous_desired
            ? ` (previous desired ${formatWire(effect.previous_desired.min_mhz)}–${formatWire(effect.previous_desired.max_mhz)} MHz)`
            : " (previous desired unknown)"}
          . Workloads continue if enforcement waits.
        </p>
      ))}
      {(preview.effects.adopted ?? []).map((effect) => (
        <p key={effect.application_id} className="library-profile-plain-note">
          Existing work on {effect.node_ids.length}{" "}
          {effect.node_ids.length === 1 ? "Spark" : "Sparks"} continues; its progress is preserved.
        </p>
      ))}
      {(preview.steps ?? []).length > 0 ? (
        <ol className="library-profile-review-steps">
          {preview.steps.map((step) => (
            <li key={formatWire(step.index)}>{step.label}</li>
          ))}
        </ol>
      ) : (preview.effects.adopted ?? []).length > 0 ? (
        <p>The load completes when continuing work finishes.</p>
      ) : (
        <p>
          {preview.effects.gpu_clocks?.length
            ? "Every workload already matches this profile."
            : "Nothing changes: every Spark already matches this profile."}
        </p>
      )}
      {counts.length > 0 && <p className="library-profile-review-summary">{counts.join(" · ")}</p>}
      <details>
        <summary>Technical detail</summary>
        <dl>
          <div>
            <dt>Plan digest</dt>
            <dd>
              <code>{preview.plan_digest}</code>
            </dd>
          </div>
          <div>
            <dt>Profile digest</dt>
            <dd>
              <code>{preview.profile_digest}</code>
            </dd>
          </div>
        </dl>
        <ul>
          {(preview.resolved_assignments ?? []).map((assignment, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: a read-only preview list without ids
            <li key={index}>
              <strong>{assignment.recipe_title}</strong> ·{" "}
              <code>
                {assignment.recipe_id}@{assignment.recipe_revision_id}
              </code>{" "}
              on {(assignment.nodes ?? []).map((node) => node.node_id).join(", ")}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

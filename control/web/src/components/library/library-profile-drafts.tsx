import { formatWire } from "../../api/contract-numeric";
import type {
  FleetProfile,
  FleetProfileNumber,
  FleetProfileInput,
  VisualFleetSnapshot,
} from "../../api/types";
import { nodeDisplayName } from "../../lib/fleet";
import { effectiveChoices } from "../ui/recipe-option-selects";
import type { OptionChoices, RecipeOption } from "../ui/recipe-option-selects";

export type AssignmentInput = NonNullable<FleetProfileInput["assignments"]>[number];
export type AssignmentDraft = {
  key: string;
  recipeSelector: string;
  assignmentName: string;
  modelVariant: string;
  desiredState: AssignmentInput["desired_state"];
  sparkIds: string[];
  optionChoices: OptionChoices;
  gpuClock?: AssignmentInput["gpu_clock"];
};
export type ProfileDraft = {
  number?: FleetProfileNumber;
  revision?: FleetProfile["revision"];
  name: string;
  description: string;
  favorite: boolean;
  installationPolicy: FleetProfileInput["installation_policy"];
  labels: FleetProfileInput["labels"];
  assignments: AssignmentDraft[];
};
type FleetEntry = { id: string; name: string; state: string };
function stringField(value: unknown, key: string): string {
  if (!value || typeof value !== "object") return "";
  const field = (value as Record<string, unknown>)[key];
  return typeof field === "string" ? field : "";
}

function profileAssignments(profile: FleetProfile): AssignmentDraft[] {
  return (profile.definition.assignments ?? []).map((assignment, index) => ({
    key: `${assignment.recipe_selector}-${index}`,
    recipeSelector: assignment.recipe_selector,
    assignmentName: assignment.assignment_name ?? "",
    modelVariant: assignment.model_variant ?? "",
    desiredState: assignment.desired_state,
    sparkIds: [...assignment.spark_ids].sort(),
    optionChoices: { ...(assignment.option_choices ?? {}) },
    gpuClock: assignment.gpu_clock,
  }));
}

export function draftFromProfile(profile: FleetProfile): ProfileDraft {
  const definition = profile.definition;
  return {
    number: profile.number,
    revision: profile.revision,
    name: definition.name ?? "",
    description: definition.description ?? "",
    favorite: definition.favorite ?? false,
    installationPolicy: definition.installation_policy,
    labels: definition.labels,
    assignments: profileAssignments(profile),
  };
}

export function blankDraft(): ProfileDraft {
  return {
    name: "New fleet profile",
    description: "A complete desired setup for the fleet.",
    favorite: false,
    installationPolicy: "keep-cached",
    labels: {},
    assignments: [],
  };
}

/**
 * Unsaved edits are kept per profile for the session, so selecting another
 * profile or navigating away never silently discards them.
 */
export const unsavedDrafts = new Map<string, ProfileDraft>();
/** Test seam: forget every kept unsaved draft. */
export const forgetUnsavedProfileDrafts = () => unsavedDrafts.clear();
export const draftKey = (draft: Pick<ProfileDraft, "number">) =>
  draft.number === undefined ? "new" : formatWire(draft.number);
const comparable = (draft: ProfileDraft) =>
  JSON.stringify({
    ...draft,
    number: draft.number === undefined ? null : formatWire(draft.number),
    revision: draft.revision === undefined ? null : formatWire(draft.revision),
    assignments: draft.assignments.map(({ key: _key, ...rest }) => rest),
  });
export function isDirty(draft: ProfileDraft, profile: FleetProfile | undefined): boolean {
  if (draft.number === undefined)
    return draft.assignments.length > 0 || comparable(draft) !== comparable(blankDraft());
  return !profile || comparable(draft) !== comparable(draftFromProfile(profile));
}
export function fleetEntries(
  profile: FleetProfile | undefined,
  fleet: VisualFleetSnapshot | undefined,
): FleetEntry[] {
  if (profile)
    return (profile.fleet ?? []).flatMap((item) => {
      const id = stringField(item, "selector");
      if (!id) return [];
      return [
        {
          id,
          name: stringField(item, "display_name") || id,
          state: stringField(item, "state") || "Unknown",
        },
      ];
    });
  return (
    fleet?.nodes.map((node) => ({ id: node.id, name: nodeDisplayName(node), state: "Observed" })) ??
    []
  );
}

export function inputFromDraft(
  draft: ProfileDraft,
  optionsBySelector: Record<string, RecipeOption[]> = {},
): FleetProfileInput {
  const input: FleetProfileInput = {
    name: draft.name,
    description: draft.description,
    expected_revision: draft.revision ?? 0,
    ...(draft.installationPolicy ? { installation_policy: draft.installationPolicy } : {}),
    ...(draft.labels ? { labels: draft.labels } : {}),
    favorite: draft.favorite,
    assignments: draft.assignments.map((assignment) => {
      const selectorOptions = optionsBySelector[assignment.recipeSelector.trim()];
      return {
        recipe_selector: assignment.recipeSelector.trim(),
        spark_ids: [...new Set(assignment.sparkIds)].sort(),
        ...(assignment.assignmentName.trim()
          ? { assignment_name: assignment.assignmentName.trim() }
          : {}),
        ...(assignment.modelVariant.trim()
          ? { model_variant: assignment.modelVariant.trim() }
          : {}),
        ...(assignment.desiredState ? { desired_state: assignment.desiredState } : {}),
        ...(assignment.gpuClock ? { gpu_clock: assignment.gpuClock } : {}),
        // Every option the recipe declares is sent explicitly, defaults included.
        option_choices: selectorOptions
          ? effectiveChoices(selectorOptions, assignment.optionChoices)
          : assignment.optionChoices,
      };
    }),
  };
  return input;
}

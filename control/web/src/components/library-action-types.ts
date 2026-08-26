import type {LibraryRecipeDetail} from "../api/types";

type PlacementTarget = LibraryRecipeDetail["placement"][number]["recommendations"][number]["preview_targets"][number];
export type LibraryPlacementGroup = LibraryRecipeDetail["placement"][number]["recommendations"][number];

export type LibraryActionTarget =
  | PlacementTarget
  | {kind: "stop"; runId: string}
  | {kind: "uninstall"; installationId: string};

export type LibraryActionName = "Build" | "Mapping" | "Distribute" | "Install" | "Load" | "Stop" | "Remove";

export type LibraryActionReview = {
  evidence?: LibraryPlacementGroup;
  target: LibraryActionTarget;
};

export function actionName(target: LibraryActionTarget): LibraryActionName {
  if (target.kind === "build") return "Build";
  if (target.kind === "mapping") return "Mapping";
  if (target.kind === "image_distribution") return "Distribute";
  if (target.kind === "install") return "Install";
  if (target.kind === "run") return "Load";
  if (target.kind === "stop") return "Stop";
  return "Remove";
}

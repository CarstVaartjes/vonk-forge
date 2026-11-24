import type {
  ArtifactJobParameter,
  ArtifactJobParameterValue,
  ArtifactJobInterface,
} from "../../api/types";
import {
  compareWire,
  contractType,
  formatWire,
  isWireNumber,
  materialize,
  parseContractJson,
  type WireNumber,
} from "../../api/contract-numeric";
import type { JobRecipeInterface } from "../../lib/artifact-job";

export const CONTROLLER_FILE_LIMIT = 512 * 1024 * 1024;
export const CONTROLLER_TOTAL_LIMIT = 1024 * 1024 * 1024;
export function wireMinimum<T extends WireNumber>(a: T, b: T): T {
  return compareWire(a, b) <= 0 ? a : b;
}
export function wireMaximum<T extends WireNumber>(a: T, b: T): T {
  return compareWire(a, b) >= 0 ? a : b;
}
// Request transport fields have Controller-owned bounds within safe integers.
// Conversion is allowed only after exact validation against that owner's bound.
export function boundedRequestInteger(value: WireNumber, maximum: number): number {
  const native = Number(formatWire(value));
  if (
    !contractType(value, ["integer"]) ||
    compareWire(value, 1) < 0 ||
    compareWire(value, maximum) > 0 ||
    !Number.isSafeInteger(native) ||
    compareWire(value, native) !== 0
  ) {
    throw new Error("Request integer is outside the Controller transport boundary");
  }
  return native;
}
export function integerInput(value: string): WireNumber | string {
  if (!/^-?(?:0|[1-9][0-9]*)$/.test(value)) return value;
  const parsed = materialize(parseContractJson(value));
  return isWireNumber(parsed) && contractType(parsed, ["integer"]) ? parsed : value;
}
type RecipeInput = NonNullable<JobRecipeInterface["input"]>;
export type RecipeInputSlot = NonNullable<RecipeInput["slots"]>[number];
// A knob value as the form holds it: typed, or the text being edited while it is not yet valid.
export type Scalar = ArtifactJobParameterValue;
type RecipeParameter = ArtifactJobParameter;
export type ContractState =
  | { kind: "awaiting" }
  | { kind: "received"; parameters: RecipeParameter[] }
  | { kind: "unreadable"; detail: string };
export const outputMedia: Record<ArtifactJobInterface, string[]> = {
  "image-job": ["image/png", "image/jpeg", "image/webp"],
  "audio-job": ["audio/wav", "audio/mpeg", "audio/flac"],
  "video-job": ["video/mp4", "video/webm"],
  "mesh-job": ["model/gltf-binary", "model/gltf+json"],
  "artifact-job": [
    "application/json",
    "text/plain",
    "application/octet-stream",
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
  ],
};

export function filename(value: string): string {
  const clean = value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return (clean || "input").slice(0, 128);
}

export function scalarText(value: Scalar): string {
  return isWireNumber(value) ? formatWire(value) : String(value);
}

export function parameterDefaults(parameters: RecipeParameter[]): Record<string, Scalar> {
  return Object.fromEntries(parameters.map((parameter) => [parameter.name, parameter.default]));
}

export function parameterRange(parameter: RecipeParameter): string | undefined {
  if (parameter.type !== "integer" && parameter.type !== "float") return undefined;
  const { minimum, maximum, step } = parameter;
  const bounds =
    minimum != null && maximum != null
      ? `${scalarText(minimum)} to ${scalarText(maximum)}`
      : minimum != null
        ? `at least ${scalarText(minimum)}`
        : maximum != null
          ? `at most ${scalarText(maximum)}`
          : "";
  if (!bounds) return undefined;
  return step != null ? `${bounds}, in steps of ${scalarText(step)}` : bounds;
}

// Only whether the text parses; range and step are the Controller's to refuse.
export function parameterError(parameter: RecipeParameter, value: Scalar): string | undefined {
  if (parameter.type === "integer" && !contractType(value, ["integer"]))
    return "Enter a whole number.";
  if (
    parameter.type === "float" &&
    typeof value === "string" &&
    (value.trim() === "" || !Number.isFinite(Number(value)))
  )
    return "Enter a number.";
  return undefined;
}

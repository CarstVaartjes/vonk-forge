import { compareWire, materialize, parseContractJson } from "../../api/contract-numeric";
import type { WireNumber } from "../../api/contract-numeric";
import { validateControlParameters } from "../../api/contract-json";
import { ProfileDocumentState } from "../../api/vocabulary.generated";
import { readableProfile } from "../../api/types";
import type { FleetProfileNumber, FleetProfileRead } from "../../api/types";

const requestedProfile = (): FleetProfileNumber | undefined => {
  const token = new URLSearchParams(location.search).get("profile");
  if (!token || !/^[1-9][0-9]*$/.test(token)) return undefined;
  const value = materialize(parseContractJson(token));
  validateControlParameters("GET", `/api/profile/${token}`, { path: { number: value } });
  if (typeof value !== "number")
    throw new Error("The profile number cannot be represented by the profile contract.");
  return value;
};
export const sameNumber = (left: WireNumber, right: WireNumber | undefined) =>
  right !== undefined && compareWire(left, right) === 0;

/** The profile the page opens on: the requested one, else the loaded one, else the first. */
export function openingProfile(profiles: FleetProfileRead[]) {
  const requested = requestedProfile();
  const readable = profiles.filter(readableProfile);
  return (
    readable.find((profile) => sameNumber(profile.number, requested)) ??
    readable.find((profile) => profile.status === ProfileDocumentState.LOADED) ??
    readable[0]
  );
}

/** The list pins the loaded profile on top, then orders by number. */
export const loadedFirst = (left: FleetProfileRead, right: FleetProfileRead) =>
  Number(right.status === ProfileDocumentState.LOADED) -
    Number(left.status === ProfileDocumentState.LOADED) || compareWire(left.number, right.number);

/** The profile state shown to people: the Controller's word, spaces for hyphens. */
export const profileStateLabel = (status: string | undefined) =>
  status?.replaceAll("-", " ") ?? "Status unavailable";

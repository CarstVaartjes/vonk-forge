import type {LibraryRecipeDetail} from "../api/types";

type Reason = LibraryRecipeDetail["reasons"][number];

export function LibraryReasons({reasons}: {reasons: Reason[]}) {
  if (reasons.length === 0) return null;
  return <ul className="library-reasons">{reasons.map((reason, index) => <li key={`${reason.code}-${index}`} className={`reason-${reason.severity}`}><strong>{reason.code}</strong><span>{reason.detail}</span></li>)}</ul>;
}

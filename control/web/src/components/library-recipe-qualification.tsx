import type { RecipeQualification } from "../api/types";
import { compareWire, formatWire, type WireNumber } from "../api/contract-numeric";
import { QualificationOutcome, QualificationStatus } from "../api/vocabulary.generated";
import { Time } from "./time";
import { useStatusMeaning } from "./library-vocabulary";

type PublishedStatus = RecipeQualification["status"];
type QualificationRecord = NonNullable<RecipeQualification["last_pass"]>;

/** One tone per published status, for the badge colour only. */
type QualificationTone = "pass" | "recheck" | "fail" | "unverified";
const QUALIFICATION_TONES: Record<PublishedStatus, QualificationTone> = {
  [QualificationStatus.WORKING]: "pass",
  [QualificationStatus.RECHECK]: "recheck",
  [QualificationStatus.FIX_TO_VERIFY]: "recheck",
  [QualificationStatus.FAILED_RECIPE]: "fail",
  [QualificationStatus.NOT_YET_VERIFIED]: "unverified",
};

/** The published status as shown; anything missing or unknown is not yet verified. */
export function qualificationStatus(
  qualification: RecipeQualification | null | undefined,
): PublishedStatus {
  const status = qualification?.status;
  return status && Object.hasOwn(QUALIFICATION_TONES, status)
    ? status
    : QualificationStatus.NOT_YET_VERIFIED;
}

/** The hardware status badge. Informative: nothing in the Library gates on it. */
export function RecipeQualificationBadge({
  qualification,
}: {
  qualification: RecipeQualification | null | undefined;
}) {
  const status = qualificationStatus(qualification);
  const meaning = useStatusMeaning();
  return (
    <span
      className="library-badge"
      data-badge="qualification"
      data-qualification={QUALIFICATION_TONES[status]}
      title={meaning(status)}
    >
      {status}
    </span>
  );
}

function sparks(count: WireNumber): string {
  return `${formatWire(count)} Spark${compareWire(count, 1) === 0 ? "" : "s"}`;
}

function newest(qualification: RecipeQualification): QualificationRecord | null {
  const pass = qualification.last_pass ?? null;
  const failure = qualification.last_failure ?? null;
  if (pass === null || failure === null) return pass ?? failure;
  return Date.parse(failure.tested_at) > Date.parse(pass.tested_at) ? failure : pass;
}

/** "Last passed <date> on <sparks>" or "Last failure: <reason>", newest evidence first. */
export function RecipeQualificationEvidence({
  qualification,
}: {
  qualification: RecipeQualification | null | undefined;
}) {
  if (!qualification) return null;
  const record = newest(qualification);
  if (record === null) {
    return qualification.reason ? (
      <small data-qualification-evidence="reason">{qualification.reason}</small>
    ) : null;
  }
  if (record.outcome === QualificationOutcome.PASSED) {
    const count = record.platform.spark_count;
    return (
      <small data-qualification-evidence="pass">
        Last passed <Time value={record.tested_at} />
        {count == null ? "" : ` on ${sparks(count)}`}
      </small>
    );
  }
  return (
    <small data-qualification-evidence="failure">
      Last failure: {record.cause_summary} (<Time value={record.tested_at} />)
    </small>
  );
}

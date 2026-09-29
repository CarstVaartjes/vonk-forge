import {exactTime, relativeTime} from "../lib/time";

/** Relative time with the exact time on hover. */
export function Time({value}: {value: string | null | undefined}) {
  if (!value) return <>never</>;
  return <time dateTime={value} title={exactTime(value) ?? undefined}>{relativeTime(value, new Date()) ?? value}</time>;
}

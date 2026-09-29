const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [["day", 86_400], ["hour", 3_600], ["minute", 60], ["second", 1]];

export function relativeTime(iso: string, now = new Date()): string {
  const seconds = Math.round((Date.parse(iso) - now.getTime()) / 1000);
  if (!Number.isFinite(seconds)) return iso;
  const [unit, size] = UNITS.find(([, size]) => Math.abs(seconds) >= size) ?? UNITS[3]!;
  return new Intl.RelativeTimeFormat("en", {numeric: "auto"}).format(Math.trunc(seconds / size), unit);
}

/** Relative time with the exact time on hover. */
export function Time({value}: {value: string | null | undefined}) {
  if (!value) return <>never</>;
  return <time dateTime={value} title={new Date(value).toLocaleString()}>{relativeTime(value)}</time>;
}

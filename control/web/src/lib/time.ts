export function exactTime(value: string): string | null {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "long"}).format(parsed);
}

export function relativeTime(value: string, now: Date): string | null {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  const seconds = Math.round((parsed.getTime() - now.getTime()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, {numeric: "auto"});
  const ranges: Array<[number, Intl.RelativeTimeFormatUnit]> = [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"]];
  let valueAtRange = seconds;
  for (const [boundary, unit] of ranges) {
    if (Math.abs(valueAtRange) < boundary) return formatter.format(valueAtRange, unit);
    valueAtRange = Math.round(valueAtRange / boundary);
  }
  return formatter.format(valueAtRange, "week");
}

/** A UTC timestamp in the form `vonkctl` prints ("2026-11-12 08:00:00+00:00"), for text shared with the CLI. */
export function utcTime(value: string): string | null {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  return `${parsed.toISOString().slice(0, 19).replace("T", " ")}+00:00`;
}

/** The Spark identity colours, in the enrolment order; every chart and card gives a Spark the same one. */
export const SERIES_COLORS = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
] as const;

export function sparkColor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length] as string;
}

/** A linear map from a data domain to a pixel range. A flat domain maps to the range start. */
export function linearScale(
  [d0, d1]: readonly [number, number],
  [r0, r1]: readonly [number, number],
): (value: number) => number {
  const span = d1 - d0;
  return (value) => (span === 0 ? r0 : r0 + ((r1 - r0) * (value - d0)) / span);
}

/** Clean tick values (1, 2 or 5 times a power of ten) that cover [min, max]; the last tick is at least max. */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min, min + 1];
  const rough = (max - min) / count;
  const power = 10 ** Math.floor(Math.log10(rough));
  const step = ([1, 2, 2.5, 5, 10].find((factor) => factor * power >= rough) ?? 10) * power;
  const first = Math.floor(min / step) * step;
  const ticks: number[] = [];
  for (let tick = first; tick < max + step; tick += step) {
    ticks.push(Number(tick.toPrecision(12)));
    if (tick >= max) break;
  }
  return ticks;
}

/** Local clock text for a Unix-seconds timestamp: time of day, or date and time on spans over two days. */
export function timeLabel(seconds: number, spanSeconds: number): string {
  const options: Intl.DateTimeFormatOptions =
    spanSeconds > 86400 * 2
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" };
  return new Date(seconds * 1000).toLocaleString([], options);
}

export type Point = { t: number; v: number | null };
export type Run = [number, number][];

/** Consecutive present values as pixel runs; a missing value ends a run, so no line or fill crosses a gap. */
export function runsOf(
  points: readonly Point[],
  x: (t: number) => number,
  y: (v: number) => number,
): Run[] {
  const runs: Run[] = [];
  let run: Run = [];
  for (const point of points) {
    if (point.v === null) {
      if (run.length) runs.push(run);
      run = [];
    } else run.push([x(point.t), y(point.v)]);
  }
  if (run.length) runs.push(run);
  return runs;
}

/** An SVG line through each run; single points draw nothing. */
export const linePath = (runs: readonly Run[]): string =>
  runs
    .filter((run) => run.length > 1)
    .map((run) =>
      run.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`).join(""),
    )
    .join("");

/** A wash from each run down to the baseline, closed at that run's own ends. */
export const washPath = (runs: readonly Run[], baseline: number): string =>
  runs
    .filter((run) => run.length > 1)
    .map((run) => {
      const line = linePath([run]);
      return `${line}L${run[run.length - 1][0].toFixed(1)} ${baseline}L${run[0][0].toFixed(1)} ${baseline}Z`;
    })
    .join("");

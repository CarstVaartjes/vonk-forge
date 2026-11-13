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

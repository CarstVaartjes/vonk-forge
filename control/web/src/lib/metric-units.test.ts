import { readFileSync } from "node:fs";
import { UNITS, formatValue } from "./metric-units";

test("every shared case reads the same words as `vonkctl metrics`", () => {
  const vectors = JSON.parse(readFileSync("../../tests/fixtures/metrics-wording.json", "utf8")) as {
    cases: { unit: keyof typeof UNITS; value: number | null; text: string }[];
  };
  for (const { unit, value, text } of vectors.cases) {
    if (value === null) continue; // "No data in this range" is drawn by the chart, not by a number
    if (!(unit in UNITS)) continue; // a unit the Controller does not serve yet
    const { symbol, scale } = UNITS[unit];
    expect(formatValue(value / scale, symbol), `${unit} ${value}`).toBe(text);
  }
});

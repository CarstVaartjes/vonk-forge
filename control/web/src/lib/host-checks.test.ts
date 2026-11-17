import { readFileSync } from "node:fs";
import { type HostCheckAdvice, hostCheckLines } from "./host-checks";

test("every shared vector reads the same lines as `vonkctl fleet detail`", () => {
  const vectors = JSON.parse(
    readFileSync("../../tests/fixtures/fleet-wording-host-checks.json", "utf8"),
  ) as { cases: { advice: HostCheckAdvice; lines: string[] }[] };
  for (const { advice, lines } of vectors.cases)
    expect(hostCheckLines(advice), advice.check).toEqual(lines);
});

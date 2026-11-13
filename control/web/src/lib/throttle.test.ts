import { readFileSync } from "node:fs";
import { throttleReasons } from "./throttle";

test("every shared vector reads the same words as `vonkctl fleet detail`", () => {
  const vectors = JSON.parse(
    readFileSync("../../tests/fixtures/fleet-wording-throttle.json", "utf8"),
  ) as { cases: { reasons: number; words: string }[] };
  for (const { reasons, words } of vectors.cases)
    expect(throttleReasons(reasons).words, String(reasons)).toBe(words);
});

test("only a thermal or hardware slowdown is throttling; idle and a power cap are the clock working as designed", () => {
  expect(throttleReasons(1).mood).toBe("clear");
  expect(throttleReasons(4).mood).toBe("clear");
  expect(throttleReasons(32).mood).toBe("throttled");
  expect(throttleReasons(512).mood).toBe("unrecognised");
});

import { expect, test } from "vitest";
import { formatDate } from "./time";

test("a calendar date reads day, short month, year", () => {
  expect(formatDate(new Date(2026, 10, 20))).toBe("20 Nov 2026");
  expect(formatDate(new Date(2026, 8, 3))).toBe("3 Sep 2026");
});

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { render, screen } from "@testing-library/react";
import { NoReading } from "./no-reading";

const here = import.meta.dirname;
const root = join(here, "..", "..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

test("a figure with no reading is drawn as a muted dash with the words kept for assistive tech", () => {
  render(<NoReading hint="No token reading yet" />);
  expect(screen.getByText("—")).toHaveAttribute("aria-hidden", "true");
  expect(screen.getByText("No token reading yet")).toBeVisible();
});

// lib/fleet.ts keeps the text for its plain-string formatters (formatBytes and friends, used in sentences and attributes);
// every component draws a missing reading through NoReading.
test("only no-reading.tsx and the lib/fleet.ts formatters name the not-reported text", () => {
  const allowed = new Set([join(root, "lib", "fleet.ts"), join(here, "no-reading.tsx")]);
  const offenders = sources(root).filter(
    (file) => !allowed.has(file) && readFileSync(file, "utf8").includes("NOT_REPORTED"),
  );
  expect(offenders).toEqual([]);
});

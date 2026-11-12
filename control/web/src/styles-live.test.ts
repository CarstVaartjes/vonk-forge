import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const src = join(import.meta.dirname);

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? files(path) : [path];
  });
}

test("every class a stylesheet selects is named by production source", () => {
  // Break caught: a removed component leaves its CSS behind, so the stylesheets
  // grow with rules no element can match (DESIGN.md then describes them as product).
  const all = files(src);
  const production = all
    .filter((path) => /\.(tsx?|html)$/.test(path) && !/generated|\.test\.|\.spec\./.test(path))
    .map((path) => readFileSync(path, "utf8"))
    .join("\n");
  const prefixes = [...production.matchAll(/className=\{`([^`]*)`\}/g)].flatMap((match) =>
    match[1]
      .split(/\s+/)
      .filter((token) => token.indexOf("${") > 0)
      .map((token) => token.slice(0, token.indexOf("${"))),
  );
  const named = (name: string) =>
    new RegExp(`(?<![\\w-])${name.replace(/-/g, "\\-")}(?![\\w-])`).test(production) ||
    prefixes.some((prefix) => name.startsWith(prefix));
  const dead = all
    .filter((path) => path.endsWith(".css"))
    .flatMap((path) => {
      const selectors = readFileSync(path, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/\{[^{}]*\}/g, "{}")
        .replace(/\{[^{}]*\}/g, "{}")
        .split(/[{}]/)
        .filter((text) => text.trim() && !text.trim().startsWith("@"))
        .map((text) =>
          text
            .replace(/\[[^\]]*\]/g, "")
            .replace(/:(?:not|is|where|has)\((?:[^()]|\([^()]*\))*\)/g, ""),
        );
      return selectors
        .flatMap((selector) => [...selector.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]))
        .filter((name) => !named(name))
        .map((name) => `${path.slice(src.length + 1)}: .${name}`);
    });
  expect([...new Set(dead)]).toEqual([]);
});

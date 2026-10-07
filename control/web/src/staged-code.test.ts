import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, test } from "vitest";

test("generated-only commit checks skip formatting and still reject invalid types", () => {
  // The real Biome/TypeScript tools catch both rejecting excluded generated
  // files and accidentally skipping their compiler check.
  const workspace = resolve(process.cwd(), "../..");
  const root = mkdtempSync(join(tmpdir(), "vonk-staged-generated-"));
  try {
    const web = join(root, "control/web");
    mkdirSync(join(root, "scripts"), { recursive: true });
    mkdirSync(join(web, "src/api"), { recursive: true });
    for (const script of ["check-staged-code", "check-staged-python"]) {
      copyFileSync(join(workspace, "scripts", script), join(root, "scripts", script));
    }
    copyFileSync(join(process.cwd(), "biome.json"), join(web, "biome.json"));
    symlinkSync(join(process.cwd(), "node_modules"), join(web, "node_modules"), "dir");
    writeFileSync(
      join(web, "package.json"),
      JSON.stringify({ scripts: { typecheck: "tsc --noEmit --pretty false" } }),
    );
    writeFileSync(
      join(web, "tsconfig.json"),
      JSON.stringify({
        compilerOptions: { strict: true, skipLibCheck: true, types: [] },
        files: ["src/api/generated.d.ts", "src/consumer.ts"],
      }),
    );
    execFileSync("git", ["init", "--quiet"], { cwd: root, timeout: 5000 });
    const generated = join(web, "src/api/generated.d.ts");
    writeFileSync(
      join(web, "src/consumer.ts"),
      'import type { CurrentWire } from "./api/generated";\nexport const value: CurrentWire = "ready";\n',
    );
    const check = () =>
      spawnSync("python3", ["scripts/check-staged-code"], {
        cwd: root,
        encoding: "utf8",
        timeout: 10_000,
      });
    writeFileSync(generated, "export type CurrentWire = string;\n");
    execFileSync("git", ["add", "control/web/src/api/generated.d.ts"], {
      cwd: root,
      timeout: 5000,
    });
    const valid = check();
    expect(valid.status).toBe(0);

    writeFileSync(generated, "export type CurrentWire = number;\n");
    execFileSync("git", ["add", "control/web/src/api/generated.d.ts"], {
      cwd: root,
      timeout: 5000,
    });
    const invalid = check();
    expect(invalid.status).not.toBe(0);
    expect(invalid.stdout + invalid.stderr).toContain("TS2322");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

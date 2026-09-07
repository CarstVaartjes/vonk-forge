import type {LibraryOperation} from "../api/types";
import {operationJobId} from "./library-operation-progress";

function operation(result: LibraryOperation["result"]): LibraryOperation {
  return {
    id: "00000000-0000-4000-8000-000000000501",
    kind: "recipe.install",
    nodes: ["spk_" + "1".repeat(32)],
    owner_id: "00000000-0000-4000-8000-000000000502",
    plan_digest: "a".repeat(64),
    state: "succeeded",
    result,
  };
}

test("narrows operation result before reading a job identity", () => {
  expect(operationJobId(operation({activated: true}))).toBeUndefined();
  expect(operationJobId(operation({
    evidence: {elapsed_milliseconds: 10, peak_memory_bytes: null},
    exit_code: 0,
    job_id: "00000000-0000-4000-8000-000000000503",
    output_manifest: {
      files: [],
      manifest_sha256: "b".repeat(64),
      schema_version: 1,
      total_bytes: 0,
    },
  }))).toBe("00000000-0000-4000-8000-000000000503");
});

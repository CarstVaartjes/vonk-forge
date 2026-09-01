import type {AgentRepairManifest} from "../api/types";
import {parseAgentRepairManifest} from "./agent-repair-manifest";

const NODE = `spk_${"a".repeat(32)}`;
const AUTHORITY = "1".repeat(64);
const PACKAGE_SHA = "2".repeat(64);

function manifest(): AgentRepairManifest {
  return {
    schema_version: 2,
    kind: "agent-upgrade-repair",
    node_id: NODE,
    authority_sha256: AUTHORITY,
    package: {
      architecture: "linux-arm64",
      package_bytes: 6_000_000,
      package_sha256: PACKAGE_SHA,
      package_signature: "8".repeat(128),
      package_url: `https://install.vonkforge.ai/repair-capsules/${NODE}/${AUTHORITY}/${PACKAGE_SHA}/vonk-forge-agent.deb`,
      package_version: "0.1.0~dev.382+gd1cef9c7d1ce",
      schema_version: 1,
      target_binary_digest: "a".repeat(64),
      target_build_digest: `sha256:${"9".repeat(64)}`,
    },
  };
}

it("accepts only the exact node-bound immutable repair contract", () => {
  expect(parseAgentRepairManifest(JSON.stringify(manifest()), NODE)).toEqual({ok: true, manifest: manifest()});
});

it.each([
  ["malformed JSON", "{", "not valid JSON"],
  ["wrong node", JSON.stringify({...manifest(), node_id: `spk_${"b".repeat(32)}`}), "not spk_aaaaaaaa"],
  ["unknown field", JSON.stringify({...manifest(), unexpected: true}), "missing or unknown"],
  ["legacy schema", JSON.stringify({...manifest(), schema_version: 1}), "identity is invalid"],
  ["mutable URL", JSON.stringify({...manifest(), package: {...manifest().package, package_url: "https://install.vonkforge.ai/repair-capsules/latest/vonk-forge-agent.deb"}}), "not the canonical"],
])("rejects %s", (_name, document, message) => {
  const result = parseAgentRepairManifest(document, NODE);
  expect(result.ok).toBe(false);
  if (!result.ok) expect(result.error).toContain(message);
});

import type {AgentRepairManifest, AgentUpgradePackage} from "../api/types";

const SHA256 = /^[0-9a-f]{64}$/;
const SIGNATURE = /^[0-9a-f]{128}$/;
const BUILD_DIGEST = /^sha256:[0-9a-f]{64}$/;
const VERSION = /^[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}$/;
const NODE_ID = /^spk_[0-9a-f]{32}$/;

type ParseResult = {ok: true; manifest: AgentRepairManifest} | {ok: false; error: string};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function validPackage(value: unknown): value is AgentUpgradePackage {
  if (!record(value) || !exactKeys(value, ["architecture", "package_bytes", "package_sha256", "package_signature", "package_url", "package_version", "schema_version", "target_binary_digest", "target_build_digest"])) return false;
  return value.schema_version === 1 && value.architecture === "linux-arm64" &&
    typeof value.package_bytes === "number" && Number.isSafeInteger(value.package_bytes) && value.package_bytes >= 1 && value.package_bytes <= 1024 ** 3 &&
    typeof value.package_sha256 === "string" && SHA256.test(value.package_sha256) &&
    typeof value.package_signature === "string" && SIGNATURE.test(value.package_signature) &&
    typeof value.package_url === "string" && value.package_url.length <= 2048 &&
    typeof value.package_version === "string" && VERSION.test(value.package_version) &&
    typeof value.target_binary_digest === "string" && SHA256.test(value.target_binary_digest) &&
    typeof value.target_build_digest === "string" && BUILD_DIGEST.test(value.target_build_digest);
}

export function parseAgentRepairManifest(text: string, selectedNodeId: string): ParseResult {
  if (!text.trim()) return {ok: false, error: "Paste or choose a node-bound repair manifest."};
  if (new TextEncoder().encode(text).byteLength > 256 * 1024) return {ok: false, error: "The repair manifest is larger than 256 KiB."};
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return {ok: false, error: "The repair manifest is not valid JSON."};
  }
  if (!record(value) || !exactKeys(value, ["schema_version", "kind", "node_id", "authority_sha256", "package"])) {
    return {ok: false, error: "The repair manifest has missing or unknown top-level fields."};
  }
  if (value.schema_version !== 2 || value.kind !== "agent-upgrade-repair" || typeof value.node_id !== "string" || !NODE_ID.test(value.node_id) || typeof value.authority_sha256 !== "string" || !SHA256.test(value.authority_sha256)) {
    return {ok: false, error: "The repair manifest identity is invalid."};
  }
  if (value.node_id !== selectedNodeId) return {ok: false, error: `This repair manifest is bound to ${value.node_id}, not ${selectedNodeId}.`};
  if (!validPackage(value.package)) return {ok: false, error: "The repair package descriptor is invalid."};
  const expectedUrl = `https://install.vonkforge.ai/repair-capsules/${value.node_id}/${value.authority_sha256}/${value.package.package_sha256}/vonk-forge-agent.deb`;
  if (value.package.package_url !== expectedUrl) return {ok: false, error: "The repair package URL is not the canonical node-bound immutable URL."};
  return {ok: true, manifest: value as AgentRepairManifest};
}

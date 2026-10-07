import {formatWire, stringifyContractJson} from "../api/contract-numeric";
import type {ControlApi, FleetProfileNumber} from "../api/types";
import {safeErrorText} from "../lib/error-display";
import {useToast} from "./toast";

/** Download the exact saved profile definition, as `vonkctl profile export` writes it. */
export function ProfileExport({api, number}: {api: Pick<ControlApi, "profileDefinition">; number: FleetProfileNumber}) {
  const toast = useToast();
  async function download() {
    try {
      const value = await api.profileDefinition(number);
      if (value.definition === null) {
        throw new Error(`${value.projection_issue?.detail} ${value.projection_issue?.next_action}`);
      }
      const url = URL.createObjectURL(new Blob([stringifyContractJson(value.definition) + "\n"], {type: "application/json"}));
      const link = document.createElement("a");
      link.href = url; link.download = `profile-${formatWire(number)}.json`; link.click();
      URL.revokeObjectURL(url);
      toast.success(`Exported profile-${formatWire(number)}.json.`);
    } catch (value) { toast.error(safeErrorText(value instanceof Error ? value.message : "Export failed", 256)); }
  }
  return <button type="button" className="button secondary" onClick={() => void download()}>Export profile</button>;
}

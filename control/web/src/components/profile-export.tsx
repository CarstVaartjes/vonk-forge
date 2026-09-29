import type {ControlApi} from "../api/types";
import {safeErrorText} from "../lib/error-display";
import {useToast} from "./toast";

/** Download the exact saved profile definition, as `vonkctl profile export` writes it. */
export function ProfileExport({api, number}: {api: Pick<ControlApi, "profileDefinition">; number: number}) {
  const toast = useToast();
  async function download() {
    try {
      const value = await api.profileDefinition(number);
      const url = URL.createObjectURL(new Blob([JSON.stringify(value.definition, null, 2) + "\n"], {type: "application/json"}));
      const link = document.createElement("a");
      link.href = url; link.download = `profile-${number}.json`; link.click();
      URL.revokeObjectURL(url);
      toast.success(`Exported profile-${number}.json.`);
    } catch (value) { toast.error(safeErrorText(value instanceof Error ? value.message : "Export failed", 256)); }
  }
  return <button type="button" className="button secondary" onClick={() => void download()}>Export profile</button>;
}

import {useState} from "react";
import type {ControlApi} from "../api/types";

/** Download the exact saved profile definition, as `vonkctl profile export` writes it. */
export function ProfileExport({api, number}: {api: Pick<ControlApi, "profileDefinition">; number: number}) {
  const [error, setError] = useState("");
  async function download() {
    setError("");
    try {
      const value = await api.profileDefinition(number);
      const url = URL.createObjectURL(new Blob([JSON.stringify(value.definition, null, 2) + "\n"], {type: "application/json"}));
      const link = document.createElement("a");
      link.href = url; link.download = `profile-${number}.json`; link.click();
      URL.revokeObjectURL(url);
    } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Export failed"); }
  }
  return <><button type="button" className="button secondary" onClick={() => void download()}>Export profile</button>{error && <span role="alert">{error}</span>}</>;
}

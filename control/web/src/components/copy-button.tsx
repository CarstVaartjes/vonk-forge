import {useState} from "react";

export function CopyButton({label, value}: {label: string; value: string}) {
  const [state, setState] = useState<"copied" | "error" | "idle">("idle");

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      setState("error");
    }
  }

  return <span className="copy-control">
    <button type="button" className="secondary-button copy-button" onClick={() => void copy()}>Copy {label}</button>
    <span className="copy-feedback" role="status">{state === "copied" ? "Copied" : state === "error" ? "Copy unavailable" : ""}</span>
  </span>;
}

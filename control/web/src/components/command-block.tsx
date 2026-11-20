import { CopyButton } from "./copy-button";

/** A shell command to read and copy: the command in a scrolling box, with its Copy button. */
export function CommandBlock({ command }: { command: string }) {
  return (
    <div className="command-block">
      <pre>
        <code>{command}</code>
      </pre>
      <CopyButton label="command" value={command} />
    </div>
  );
}

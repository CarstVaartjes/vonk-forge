import type { ReactNode } from "react";

export type StatusTone = "neutral" | "ok" | "warn" | "bad" | "info";

export function StatusPill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: StatusTone;
}) {
  return (
    <span className={`status-pill status-pill-${tone}`}>
      <span className="status-mark" aria-hidden="true" />
      {children}
    </span>
  );
}

import type {ReactNode} from "react";

export type StatusTone = "neutral" | "healthy" | "warning" | "danger" | "info";

export function StatusPill({children, tone = "neutral"}: {children: ReactNode; tone?: StatusTone}) {
  return <span className={`status-pill status-pill-${tone}`}>{children}</span>;
}

import "./estimate.css";
import type { ReactNode } from "react";
import type { MetricSpec } from "../api/types";

/** A number the Controller marks as estimated: "≈" before it, in the faint ink. Measured values pass through. */
export function Estimate({ basis, children }: { basis: MetricSpec["basis"]; children: ReactNode }) {
  if (basis !== "estimated") return <>{children}</>;
  return (
    <>
      <span className="estimate-mark" title="Estimated">
        ≈
      </span>
      {children}
    </>
  );
}

/** The "est." tag for a headline figure; nothing for a measured one. The line under the figure says why. */
export function EstimateTag({ basis }: { basis: MetricSpec["basis"] }) {
  return basis === "estimated" ? <span className="estimate-tag">est.</span> : null;
}

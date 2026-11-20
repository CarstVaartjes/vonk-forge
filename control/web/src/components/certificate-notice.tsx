import type { MetricsSeriesResponse } from "../api/types";
import { formatTimeLeft, seriesName } from "./metrics-chart";

/** A certificate with less than this left (or already expired) is worth a banner. */
const WARN_SECONDS = 30 * 86400;

/** One line per certificate that is expired or expires soon, from the Controller's `certificate_expiry` read. */
export function expiringCertificates(
  data: MetricsSeriesResponse | null | undefined,
  names: Record<string, string>,
): string[] {
  return (data?.series ?? []).flatMap((series) => {
    const last = [...series.points].reverse().find((point) => point.value !== null);
    if (!last) return [];
    const left = Number(last.value);
    if (left >= WARN_SECONDS) return [];
    const name = seriesName(series.labels, names);
    return [
      left <= 0
        ? `${name}: certificate expired`
        : `${name}: certificate expires in ${formatTimeLeft(left)}`,
    ];
  });
}

/** One warning banner near the top of the page, absent when no certificate is close to expiry. */
export function CertificateNotice({ lines }: { lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <section className="certificate-notice" role="status" aria-label="Certificates">
      <strong>
        {lines.length === 1 ? "A certificate needs renewing" : "Certificates need renewing"}
      </strong>
      <ul>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </section>
  );
}

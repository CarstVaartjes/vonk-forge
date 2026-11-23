import type { MetricsSeriesResponse } from "../../api/types";
import { seriesName } from "./metrics-chart";

/**
 * One line per certificate that has expired, from the Controller's `certificate_expiry` read. Agent certificates
 * renew at half their life, so time left alone is no warning (the Prometheus alert covers expiry soon); an
 * expired one means renewal failed.
 */
export function expiringCertificates(
  data: MetricsSeriesResponse | null | undefined,
  names: Record<string, string>,
): string[] {
  return (data?.series ?? []).flatMap((series) => {
    const last = [...series.points].reverse().find((point) => point.value !== null);
    if (!last) return [];
    const left = Number(last.value);
    if (left > 0) return [];
    return [`${seriesName(series.labels, names)}: certificate expired`];
  });
}

/** One warning banner near the top of the page, absent when no certificate is close to expiry. */
export function CertificateNotice({ lines }: { lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <section className="certificate-notice" role="status" aria-label="Certificates">
      <strong>
        {lines.length === 1 ? "A certificate has expired" : "Certificates have expired"}
      </strong>
      <ul>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </section>
  );
}

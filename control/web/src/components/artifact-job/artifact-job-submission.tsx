import type { ArtifactJob, ArtifactJobInputRequest } from "../../api/types";
import { compareWire } from "../../api/contract-numeric";
import { isTerminal } from "../../lib/artifact-job";

/** A cancel was accepted and the job has not settled: it completes by itself, so nobody has to act. */
export function isCancelling(job: ArtifactJob): boolean {
  return job.cancel_requested_at != null && !isTerminal(job);
}

export type InputPayload = { declaration: ArtifactJobInputRequest; blob: Blob };
export type CancelRecovery = {
  jobId: string;
  runId: string;
  reason: string;
  requestId: string;
  source: "upload" | "history";
  message: string;
};

export function responseStatus(value: unknown): number | undefined {
  if (typeof value !== "object" || value === null || !("status" in value)) return undefined;
  return typeof value.status === "number" ? value.status : undefined;
}

export function mayHaveBeenAccepted(value: unknown): boolean {
  const status = responseStatus(value);
  return status === undefined || status === 0 || status >= 500;
}

export function isDefinitiveHttpRefusal(value: unknown): boolean {
  const status = responseStatus(value);
  return status !== undefined && status >= 400 && status < 500;
}

export function isAbort(value: unknown): boolean {
  return (
    (typeof DOMException !== "undefined" &&
      value instanceof DOMException &&
      value.name === "AbortError") ||
    (value instanceof Error && value.name === "AbortError")
  );
}

export function assertJobIdentity(
  job: ArtifactJob,
  expectedJobId: string | undefined,
  expectedRunId: string,
  source: string,
): void {
  if (expectedJobId && job.id !== expectedJobId) {
    throw new Error(`${source} returned another artifact job; expected ${expectedJobId}`);
  }
  if (job.run_id !== expectedRunId) {
    throw new Error(`${source} belongs to another recipe run; expected ${expectedRunId}`);
  }
}

export function errorText(value: unknown, fallback: string): string {
  return value instanceof Error ? value.message.slice(0, 256) : fallback;
}

export function cancelReceipt(job: ArtifactJob): { requestId: string; reason: string } | undefined {
  const evidence = job.result_evidence;
  if (!evidence || typeof evidence !== "object") return undefined;
  const requestId = evidence.cancel_request_id;
  const reason = evidence.cancel_reason;
  return typeof requestId === "string" && typeof reason === "string"
    ? { requestId, reason }
    : undefined;
}

export function isCancelReceipt(job: ArtifactJob, requestId: string, reason: string): boolean {
  const receipt = cancelReceipt(job);
  return (
    receipt?.requestId === requestId &&
    receipt.reason === reason &&
    (isCancelling(job) || job.state === "cancelled")
  );
}

export function recoveredInputIndexes(job: ArtifactJob, prepared: InputPayload[]): Set<number> {
  const matched = new Set<number>();
  for (const uploaded of job.input_files) {
    const index = prepared.findIndex(
      ({ declaration }) => declaration.slot === uploaded.slot && declaration.name === uploaded.name,
    );
    const expected = index < 0 ? undefined : prepared[index]?.declaration;
    if (
      !expected ||
      matched.has(index) ||
      compareWire(expected.size_bytes, uploaded.size_bytes) !== 0 ||
      expected.sha256 !== uploaded.sha256
    ) {
      throw new Error(
        `Durable job ${job.id} has an input that does not match the selected files; its exact draft cannot be reused.`,
      );
    }
    matched.add(index);
  }
  return matched;
}

export function abortError(): DOMException {
  return new DOMException("Artifact job submission was cancelled", "AbortError");
}

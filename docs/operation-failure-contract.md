# Operation failure evidence

Activity and job details compose the existing canonical failure contracts:
`AgentFailureResult` for agent-reported infrastructure failures,
`AvailabilityOperationFailure` for model-cache work, and
`OperationFailureEvidence` for Controller-owned operations. The producer family
selects its validator before the HTTP response union is serialized.

Agent failure reasons, summaries, error codes, diagnostics, and activation
receipts retain their protocol meaning and bounds. Sanitization occurs when
agent evidence is accepted. Artifact process receipts retain their full output
manifest on the artifact-job endpoint and expose the process failure reason in
Activity.

A failed attempt's "Download diagnostics" file
(`GET /api/operations/{id}/evidence?attempt=N`) is rendered on request from the
durable attempt result, job result or progress; nothing is copied or stored for
it. The file carries the summary, error code, phase, category, redacted
stdout/stderr tails and the agent's `FailureDiagnostics`. Operation responses
name it through `evidence_download.href` only while that attempt is a failure
attempt. A multi-rank artifact job's parent Job ends only after every rank has, so the
download answers as soon as the artifact job itself has failed, with its reason.

The cache writes `AvailabilityOperationFailure` directly into durable failure
state. Exception codes and recovery choices are translated once, at creation.
Reads validate this document without repairing missing fields or invalid types.
Cache details and Activity return the same failure, including recovery actions,
retry timing, capacity measurements, and the failed artifact key. A queued
retry retains its failure evidence until execution resumes.

A workload's process-exit summary (the helper's one-line account of an exited
container) may carry `gpu_xid=<code>`: the first NVIDIA Xid code the helper saw
in the kernel journal during that container's lifetime. It is evidence only and
never changes `exit_cause`.

Library feedback accepts the generated `AvailabilityOperationFailure` type.
Callers pass the operation ID and retained progress as explicit display context.
The display adapter sanitizes text and labels canonical recovery actions; it
does not interpret strings or arbitrary nested objects as failure evidence.
API request exceptions use a separate local error component with sanitized
messages and caller-owned retry actions.

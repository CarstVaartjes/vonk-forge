# Operation failure evidence

Activity and job details compose the existing canonical failure contracts:
`AgentFailureResult` for agent-reported infrastructure failures,
`AvailabilityOperationFailure` for model-cache work, and
`OperationFailureEvidence` for Controller-owned operations. The producer family
selects its validator before the HTTP response union is serialized.

Agent failure reasons, summaries, error codes, diagnostics, and activation
receipts retain their protocol meaning and bounds. Sanitization occurs when
agent evidence is accepted. Evidence-download metadata remains separate from
the agent's result. Artifact process receipts retain their full output manifest
on the artifact-job endpoint and expose the process failure reason in Activity.

The cache writes `AvailabilityOperationFailure` directly into durable failure
state. Exception codes and recovery choices are translated once, at creation.
Reads validate this document without repairing missing fields or invalid types.
Cache details and Activity return the same failure, including recovery actions,
retry timing, capacity measurements, and the failed artifact key. A queued
retry retains its failure evidence until execution resumes.

Library feedback accepts the generated `AvailabilityOperationFailure` type.
Callers pass the operation ID and retained progress as explicit display context.
The display adapter sanitizes text and labels canonical recovery actions; it
does not interpret strings or arbitrary nested objects as failure evidence.
API request exceptions use a separate local error component with sanitized
messages and caller-owned retry actions.

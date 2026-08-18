import {useId, useState} from "react";
import type {EnrollmentSummary} from "../api/types";

type EnrollmentReviewProps = {
  actionsDisabled?: boolean;
  enrollment: EnrollmentSummary;
  onApprove(enrollmentId: string): Promise<void>;
  onReject(enrollmentId: string, reason: string): Promise<void>;
};

function EvidenceItem({label, value}: {label: string; value: string | null | undefined}) {
  return <div><dt>{label}</dt><dd><code>{value ?? "—"}</code></dd></div>;
}

export function EnrollmentReview({actionsDisabled = false, enrollment, onApprove, onReject}: EnrollmentReviewProps) {
  const headingId = useId();
  const [evidenceConfirmed, setEvidenceConfirmed] = useState(false);
  const [rejectionConfirmation, setRejectionConfirmation] = useState("");
  const [rejectionReason, setRejectionReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setActionError("");
    try {
      await action();
    } catch (value) {
      setActionError(value instanceof Error ? value.message : "The enrollment decision could not be completed.");
    } finally {
      setBusy(false);
    }
  }

  return <section className="review-card" role="region" aria-labelledby={headingId}>
    <h3 id={headingId}>Enrollment evidence for <code>{enrollment.node_id}</code></h3>
    <p>Enrollment <code>{enrollment.id}</code> · <span className="status">{enrollment.state}</span></p>
    <dl className="evidence-grid">
      <EvidenceItem label="Immutable node ID" value={enrollment.node_id}/>
      <EvidenceItem label="Host-key fingerprint" value={enrollment.host_key_fingerprint}/>
      <EvidenceItem label="Hardware fingerprint" value={enrollment.hardware_fingerprint}/>
      <EvidenceItem label="Agent digest" value={enrollment.agent_digest}/>
      <EvidenceItem label="CSR public-key fingerprint" value={enrollment.csr_public_key_fingerprint}/>
      <EvidenceItem label="Boot ID" value={enrollment.boot_id}/>
      <EvidenceItem label="Created at" value={enrollment.created_at}/>
      <EvidenceItem label="Certificate fingerprint" value={enrollment.certificate_fingerprint}/>
      <EvidenceItem label="Certificate serial" value={enrollment.certificate_serial}/>
    </dl>
    <h4>Decision audit</h4>
    <dl className="evidence-grid compact">
      <EvidenceItem label="State" value={enrollment.state}/>
      <EvidenceItem label="Decision actor" value={enrollment.decision_actor}/>
      <EvidenceItem label="Decided at" value={enrollment.decided_at}/>
      <EvidenceItem label="Rejection reason" value={enrollment.rejection_reason}/>
    </dl>
    {actionError && <p role="alert">{actionError}</p>}
    {enrollment.state === "pending" && <div className="decision-grid">
      <div>
        <h4>Approve</h4>
        <p>Compare every fingerprint and digest above with trusted inventory or the node's physical console.</p>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={evidenceConfirmed}
            disabled={actionsDisabled}
            onChange={event => setEvidenceConfirmed(event.target.checked)}
          />
          I compared all fingerprints and the agent digest with trusted evidence
        </label>
        <button
          type="button"
          disabled={actionsDisabled || !evidenceConfirmed || busy}
          onClick={() => void run(() => onApprove(enrollment.id))}
        >Approve enrollment</button>
      </div>
      <div className="danger-zone">
        <h4>Reject</h4>
        <p role="alert">Rejection is an irreversible administrative decision and cannot be undone.</p>
        <label>Rejection reason
          <input
            disabled={actionsDisabled}
            value={rejectionReason}
            onChange={event => setRejectionReason(event.target.value)}
          />
        </label>
        <label>Type {enrollment.node_id} to confirm rejection
          <input
            autoComplete="off"
            disabled={actionsDisabled}
            value={rejectionConfirmation}
            onChange={event => setRejectionConfirmation(event.target.value)}
          />
        </label>
        <button
          className="danger"
          type="button"
          disabled={actionsDisabled || rejectionConfirmation !== enrollment.node_id || !rejectionReason.trim() || busy}
          onClick={() => void run(() => onReject(enrollment.id, rejectionReason.trim()))}
        >Reject enrollment</button>
      </div>
    </div>}
  </section>;
}

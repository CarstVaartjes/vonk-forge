import type {ProposalPreview} from "../api/types";
export function ProposalDiff({preview}: {preview: ProposalPreview}) {
  let patch = ""; try { patch = new TextDecoder().decode(Uint8Array.from(atob(preview.patch), c => c.charCodeAt(0))); } catch { patch = "The server returned an unreadable patch."; }
  return <section aria-labelledby="proposal-diff-title"><h3 id="proposal-diff-title">Canonical diff</h3><p><strong>Base revision:</strong> <code>{preview.base_revision}</code></p><p><strong>Proposal digest:</strong> <code>{preview.digest}</code></p><ul>{preview.validation_results.map(result => <li key={result}>{result}</li>)}</ul><pre data-testid="canonical-diff" tabIndex={0}>{patch}</pre></section>;
}

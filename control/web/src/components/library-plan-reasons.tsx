export function LibraryPlanReasons({heading, reasons}: {heading: string; reasons: {code: string; detail: string}[]}) {
  if (reasons.length === 0) return null;
  return <section className="action-reasons" aria-label={heading}>
    <h4>{heading}</h4>
    <ul>{reasons.map((reason, index) => <li key={`${reason.code}:${index}`}><strong>{reason.code}</strong><span>{reason.detail}</span></li>)}</ul>
  </section>;
}

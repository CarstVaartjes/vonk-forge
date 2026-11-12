/** A gauge with its words beside it, so colour is never the only signal. */
export function Meter({
  label,
  vital,
  note,
}: {
  label: string;
  vital: { percent: number; text: string };
  note?: string | null;
}) {
  return (
    <span className="meter-cell">
      <span
        className={vital.percent >= 90 ? "meter meter-high" : "meter"}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={vital.percent}
        aria-valuetext={vital.text}
      >
        <span style={{ width: `${vital.percent}%` }} />
      </span>
      <span>
        {vital.text}
        {note ? ` · ${note}` : ""}
      </span>
    </span>
  );
}

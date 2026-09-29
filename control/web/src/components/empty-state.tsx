type EmptyStateProps = {
  /** Heading when the list is genuinely empty, e.g. "No API keys". */
  title: string;
  /** One sentence saying what this list is. */
  description: string;
  action?: {label: string; onClick(): void};
  /** Filters, not the data, caused the empty result. */
  filtered?: boolean;
  onClearFilters?(): void;
};

/** Empty list: says what it is and offers one primary action; when filters emptied it, offers to clear them instead. */
export function EmptyState({title, description, action, filtered = false, onClearFilters}: EmptyStateProps) {
  const primary = filtered ? (onClearFilters ? {label: "Clear filters", onClick: onClearFilters} : undefined) : action;
  return <section className="fleet-empty empty-state" data-filtered={filtered || undefined}>
    <h2>{filtered ? "No results match these filters" : title}</h2>
    <p>{filtered ? "Nothing matches the search or filters you set." : description}</p>
    {primary && <button type="button" className="button" onClick={primary.onClick}>{primary.label}</button>}
  </section>;
}

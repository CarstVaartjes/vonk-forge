/** One shimmering placeholder block; the only loading shape the UI uses. */
export function Skeleton({width = "100%"}: {width?: string}) {
  return <span className="skeleton" style={{width}} aria-hidden="true"/>;
}

/** Placeholder table rows while a list loads. Announced once as busy, not per cell. */
export function SkeletonRows({columns, rows = 4, label}: {columns: number; rows?: number; label: string}) {
  return <section className="fleet-compact" aria-busy="true" aria-label={label}>
    <p className="sr-only" role="status">{label}</p>
    <div className="fleet-table-scroll"><table aria-hidden="true"><tbody>
      {Array.from({length: rows}, (_, row) => <tr key={row}>{Array.from({length: columns}, (_, column) => <td key={column}><Skeleton width={column === 0 ? "70%" : "50%"}/></td>)}</tr>)}
    </tbody></table></div>
  </section>;
}

/** Placeholder lines for a detail pane while it loads. Announced once as busy. */
export function SkeletonBlock({lines = 4, label}: {lines?: number; label: string}) {
  return <div className="skeleton-block" aria-busy="true">
    <p className="sr-only" role="status">{label}</p>
    {Array.from({length: lines}, (_, line) => <Skeleton key={line} width={line === 0 ? "45%" : line % 2 ? "90%" : "70%"}/>)}
  </div>;
}

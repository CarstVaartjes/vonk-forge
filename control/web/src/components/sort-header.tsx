import {useMemo, useState} from "react";

export type Sort<K extends string> = {key: K; descending: boolean};

/** Click-to-sort state for a table; values that are null always sort last. */
export function useSort<T, K extends string>(rows: T[], initial: Sort<K>, values: Record<K, (row: T) => string | number | null>) {
  const [sort, setSort] = useState<Sort<K>>(initial);
  const sorted = useMemo(() => {
    const value = values[sort.key];
    return [...rows].sort((a, b) => {
      const left = value(a);
      const right = value(b);
      if (left === null || right === null) return left === right ? 0 : left === null ? 1 : -1;
      const order = typeof left === "string" && typeof right === "string" ? left.localeCompare(right) : Number(left) - Number(right);
      return sort.descending ? -order : order;
    });
    // `values` is a stable literal per table; rows and sort are the inputs.
  }, [rows, sort]);
  const toggle = (key: K) => setSort(current => ({key, descending: current.key === key && !current.descending}));
  return {sorted, sort, toggle};
}

export function SortHeader<K extends string>({column, label, sort, onSort}: {column: K; label: string; sort: Sort<K>; onSort(column: K): void}) {
  const active = sort.key === column;
  return <th scope="col" aria-sort={active ? (sort.descending ? "descending" : "ascending") : "none"}>
    <button type="button" className="sort-header" onClick={() => onSort(column)}>{label}<span aria-hidden="true">{active ? (sort.descending ? " ↓" : " ↑") : ""}</span></button>
  </th>;
}

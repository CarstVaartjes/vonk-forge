import { useEffect, useState } from "react";

function selectedSpark(): string | null {
  return new URLSearchParams(location.search).get("spark");
}

/** The Spark named by `?spark=` in the address, kept in step with the back button; null closes it. */
export function useSelectedSpark(): [string | null, (id: string | null) => void] {
  const [spark, setSpark] = useState(selectedSpark);
  useEffect(() => {
    const listener = () => setSpark(selectedSpark());
    addEventListener("popstate", listener);
    return () => removeEventListener("popstate", listener);
  }, []);
  function select(id: string | null) {
    const url = new URL(location.href);
    if (id) url.searchParams.set("spark", id);
    else url.searchParams.delete("spark");
    history.pushState(null, "", `${url.pathname}${url.search}`);
    setSpark(id);
  }
  return [spark, select];
}

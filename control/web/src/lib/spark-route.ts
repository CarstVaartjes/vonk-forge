const SPARK_PATH = /^\/sparks\/([^/]+)$/;

/** The Spark's own page. The id, not the name: it survives a rename and is what the API addresses. */
export function sparkPath(id: string): string {
  return `/sparks/${encodeURIComponent(id)}`;
}

/** The Spark id a path names, or null when the path is not a Spark page. */
export function sparkRouteId(pathname: string): string | null {
  const match = SPARK_PATH.exec(pathname);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1] as string);
  } catch {
    return null;
  }
}

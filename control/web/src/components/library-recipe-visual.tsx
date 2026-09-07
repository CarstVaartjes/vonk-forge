import type {LibraryRecipeDetail} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {selectedRecipeFiles} from "./library-recipe-files";

export function LibraryRecipeVisual({document, modelDocuments = []}: {document: LibraryRecipeDetail["definition"]; modelDocuments?: LibraryRecipeDetail["model_documents"]}) {
  const selected = selectedRecipeFiles(modelDocuments);
  const modelFileSummary = selected.unresolved.length ? "Unknown · incomplete file selection" : `${selected.files.length} · ${formatBytes(selected.files.reduce((sum, file) => sum + file.size_bytes, 0))}`;
  return <section className="library-section recipe-contract" aria-label="Recipe contract"><header><h3>Exact Recipe contract</h3><span>Schema {document.schema_version}</span></header><dl className="visual-field-grid"><div><dt>Execution engine</dt><dd>{document.runtime.engine}</dd></div><div><dt>Image</dt><dd>{document.execution.mode === "image" ? document.execution.image.repository : "Built Controller image"}</dd></div><div><dt>Topology</dt><dd>{document.topology.name} · {document.topology.node_count} Spark{document.topology.node_count === 1 ? "" : "s"}</dd></div><div><dt>Interfaces</dt><dd>{document.interfaces.map(item => item.adapter).join(" · ")}</dd></div><div><dt>Selected Model files</dt><dd>{modelFileSummary}</dd></div><div><dt>Release</dt><dd>{document.release.version} · {document.release.released_at}</dd></div></dl><details><summary>Runtime details</summary><p>Entrypoint: {document.runtime.entrypoint.join(" ") || "not declared"}</p><p>Lifecycle timeout: {document.runtime.lifecycle.stop_timeout_seconds}s</p></details></section>;
}

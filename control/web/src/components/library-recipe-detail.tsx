import type {ControlApi, LibraryRecipeDetail as RecipeDetail} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {ArtifactJobWorkspace} from "./artifact-job-workspace";
import {LibraryProfileComposer} from "./library-profile-composer";
import {LibraryRecipeFit} from "./library-recipe-fit";
import {LibraryRecipeVisual} from "./library-recipe-visual";
import {LibraryRecipeAvailability} from "./library-recipe-availability";
import {recipeAttribution} from "./library-workcell";
import "./library-recipe-detail.css";

export function LibraryRecipeAuthority({api, detail, onBusyChange}: {api: ControlApi; detail: RecipeDetail; onBusyChange?(busy: boolean): void}) {
  const documents = detail.model_documents;
  const files = documents.flatMap(model => model.model_document.files);
  return <article className="library-recipe-detail" aria-labelledby="recipe-detail-heading">
    <header className="library-detail-heading"><div><span>Exact Recipe</span><h2 id="recipe-detail-heading">{detail.recipe.title}</h2><p>{detail.recipe.description}</p><small>{recipeAttribution(detail.definition)}</small></div><span className="library-schema-badge">Schema {detail.schema_version}</span></header>
    <LibraryRecipeFit detail={detail}/>
    <section className="library-section" aria-label="Models used by Recipe"><header><h3>Ordered Model inputs</h3><span>{documents.length} Model{documents.length === 1 ? "" : "s"} · {files.length} files</span></header><ol className="library-model-detail-list">{documents.map((item, index) => <li key={`${item.model_document.identity.publisher}/${item.model_document.identity.slug}`}><span className="library-order">{index + 1}</span><div><strong>{item.model_document.identity.model.title}</strong><small>{item.model_document.identity.publisher}/{item.model_document.identity.slug} · {item.selection.files.length} Recipe mount{item.selection.files.length === 1 ? "" : "s"}</small><ul>{item.model_document.files.map(file => <li key={file.id}>{file.path} · {formatBytes(file.size_bytes)} · sha256:{file.sha256.slice(0, 12)}…</li>)}</ul></div></li>)}</ol></section>
    <LibraryRecipeAvailability api={api} detail={detail} onBusyChange={onBusyChange}/>
    <ArtifactJobWorkspace api={api} detail={detail} onBusyChange={onBusyChange}/>
    <LibraryProfileComposer api={api} detail={detail}/>
    <LibraryRecipeVisual document={detail.definition} modelDocuments={documents}/>
    <section className="library-section" aria-label="Operational state"><header><h3>Controller state</h3><span>{detail.operational_state.installations.length} installations · {detail.operational_state.runs.length} runs</span></header><p>{detail.operational_state.runs.length ? "This Recipe has observed runs on the fleet." : "No active run is reported for this Recipe."}</p></section>
  </article>;
}

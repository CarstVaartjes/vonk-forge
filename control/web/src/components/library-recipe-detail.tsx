import type {MouseEvent} from "react";
import type {ControlApi, LibraryViewRecipeDetail as RecipeDetail} from "../api/types";
import {recipeLibraryPath} from "../lib/library-route";
import {formatBytes} from "../lib/fleet";
import {ArtifactJobWorkspace} from "./artifact-job-workspace";
import {LibraryProfileComposer} from "./library-profile-composer";
import {LibraryRecipeFit} from "./library-recipe-fit";
import {LibraryRecipeVisual} from "./library-recipe-visual";
import {RecipeBadges, recipeAttribution, sparkLabel} from "./library-workcell";
import "./library-recipe-detail.css";

const CACHE_WORDS: Record<string, string> = {cached: "cached", preparing: "preparing", not_cached: "not cached", failed: "cache failed", unknown: "cache unknown"};
const FIT_WORDS: Record<string, string> = {ready: "fits fleet", blocked: "does not fit fleet", unavailable: "fit unknown"};

/** Other recipes for the same model, one comparable line each. */
export function LibraryRecipeAlternatives({alternatives, onNavigate}: {alternatives: NonNullable<RecipeDetail["alternatives"]>; onNavigate?: (event: MouseEvent<HTMLAnchorElement>, path: string) => void}) {
  if (alternatives.length === 0) return null;
  return <section className="library-section" aria-label="Other recipes for this model"><header><h3>Other recipes for this model</h3><span>{alternatives.length} alternative{alternatives.length === 1 ? "" : "s"}</span></header><ul className="library-alternatives">{alternatives.map(item => {
    const href = recipeLibraryPath(item.selector);
    return <li key={item.selector}><a href={href} onClick={event => onNavigate?.(event, href)}><strong>{item.title}</strong><small>{[item.engine, sparkLabel(item.node_count), item.creator ?? "unknown creator", `v${item.version}`, CACHE_WORDS[item.cache] ?? item.cache, FIT_WORDS[item.fits_fleet] ?? item.fits_fleet].join(" · ")}</small></a></li>;
  })}</ul></section>;
}

export function LibraryRecipeAuthority({api, detail, onBusyChange, onNavigate}: {api: ControlApi; detail: RecipeDetail; onBusyChange?(busy: boolean): void; onNavigate?: (event: MouseEvent<HTMLAnchorElement>, path: string) => void}) {
  const documents = detail.model_documents;
  const files = documents.flatMap(model => model.model_document.files);
  return <article className="library-recipe-detail" aria-labelledby="recipe-detail-heading">
    <header className="library-detail-heading"><div><span>Exact Recipe</span><h2 id="recipe-detail-heading">{detail.recipe.title}</h2><p>{detail.recipe.description}</p><RecipeBadges recipe={detail.recipe}/><small>{recipeAttribution(detail.definition)}</small></div><span className="library-schema-badge">Release {detail.definition.release.version}</span></header>
    <LibraryRecipeFit detail={detail}/>
    <LibraryRecipeAlternatives alternatives={detail.alternatives ?? []} onNavigate={onNavigate}/>
    <section className="library-section" aria-label="Models used by Recipe"><header><h3>Ordered Model inputs</h3><span>{documents.length} Model{documents.length === 1 ? "" : "s"} · {files.length} files</span></header><ol className="library-model-detail-list">{documents.map((item, index) => <li key={`${item.model_document.identity.publisher}/${item.model_document.identity.slug}`}><span className="library-order">{index + 1}</span><div><strong>{item.model_document.identity.model.title}</strong><small>{item.model_document.identity.publisher}/{item.model_document.identity.slug} · {item.selection.files.length} Recipe mount{item.selection.files.length === 1 ? "" : "s"}</small><ul>{item.model_document.files.map(file => <li key={file.id}>{file.path} · {formatBytes(file.size_bytes)} · sha256:{file.sha256.slice(0, 12)}…</li>)}</ul></div></li>)}</ol></section>
    <ArtifactJobWorkspace api={api} detail={detail} onBusyChange={onBusyChange}/>
    <LibraryProfileComposer api={api} detail={detail}/>
    <LibraryRecipeVisual document={detail.definition} modelDocuments={documents}/>
    <section className="library-section" aria-label="Operational state"><header><h3>Controller state</h3><span>{detail.operational_state.installations.length} installations · {detail.operational_state.runs.length} runs</span></header><p>{detail.operational_state.runs.length ? "This Recipe has observed runs on the fleet." : "No active run is reported for this Recipe."}</p></section>
  </article>;
}

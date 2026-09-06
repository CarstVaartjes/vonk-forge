import {useEffect, useMemo, useState} from "react";
import type {ControlApi, LibraryRecipeDetail, RecipeImageAvailabilityOperation} from "../api/types";
import {availabilityFailure} from "./library-availability-feedback";
import {LibraryAvailabilityOperation, recipeAvailabilityPresentation, selectAvailabilityOperation, type AvailabilityMemberPresentation} from "./library-availability-operation";

const terminal = new Set(["succeeded", "failed", "cancelled"]);

export function LibraryRecipeAvailability({api, detail, onBusyChange}: {api: ControlApi; detail: LibraryRecipeDetail; onBusyChange?(busy: boolean): void}) {
  const revisionId = detail.recipe.recipe_revision_id;
  const [operation, setOperation] = useState<RecipeImageAvailabilityOperation>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const modelAccessUrl = detail.model_documents[0]?.model_document.provenance.source_url;
  const presentation = useMemo(() => operation ? recipeAvailabilityPresentation(operation) : undefined, [operation]);

  async function load(signal?: AbortSignal) {
    if (!api.recipeAvailabilityList) return;
    setLoading(true);
    try {
      const page = await api.recipeAvailabilityList(revisionId, undefined, undefined, signal);
      const selected = selectAvailabilityOperation(page.operations.map(recipeAvailabilityPresentation), revisionId);
      setOperation(selected ? page.operations.find(item => item.id === selected.id) : undefined);
      setError(undefined);
    } catch (value) {
      if (!signal?.aborted) setError(value);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [api, revisionId]);

  useEffect(() => {
    onBusyChange?.(Boolean(operation && !terminal.has(operation.state)));
    return () => onBusyChange?.(false);
  }, [onBusyChange, operation]);

  useEffect(() => {
    if (!operation || terminal.has(operation.state) || !api.recipeAvailabilityOperation) return;
    const timer = window.setTimeout(() => void api.recipeAvailabilityOperation!(operation.id).then(setOperation).catch(setError), 1_200);
    return () => window.clearTimeout(timer);
  }, [api, operation]);

  if (!api.recipeAvailabilityStart || !api.recipeAvailabilityList) return null;

  async function start(force: boolean) {
    setError(undefined);
    try {
      setOperation(await api.recipeAvailabilityStart({request_key: crypto.randomUUID(), recipe_revision_id: revisionId, force}));
    } catch (value) {
      setError(value);
    }
  }

  async function retry() {
    if (!operation || !api.retryRecipeAvailability) return;
    setError(undefined);
    try {
      setOperation(await api.retryRecipeAvailability(operation.id, {request_key: crypto.randomUUID()}));
    } catch (value) {
      setError(value);
    }
  }

  async function checkAccess(member: AvailabilityMemberPresentation) {
    if (!member.id || !member.artifactSetSha256 || !member.planDigest || !api.checkModelCacheAccessAndResume) {
      setError(new Error("The Model access check is missing its exact cache identity; reload the operation and try again."));
      return;
    }
    setError(undefined);
    try {
      await api.checkModelCacheAccessAndResume(member.id, {
        schema_version: 2,
        request_key: crypto.randomUUID(),
        artifact_set_sha256: member.artifactSetSha256,
        plan_digest: member.planDigest,
      });
      await load();
    } catch (value) {
      setError(value);
    }
  }

  return <section className="library-recipe-availability" aria-label="Make Recipe available">
    <header className="library-section"><div><h3>Make available</h3><p>Local Recipe means its verified runtime image is prepared on NAS. It does not mean running on Sparks.</p></div><span>Exact revision {revisionId}</span></header>
    {loading && !presentation && <p role="status">Checking durable availability…</p>}
    {Boolean(error) && <div role="alert"><LibraryAvailabilityOperation operation={{id: "error", requestId: "error", recipeRevisionId: revisionId, state: "failed", attempt: 0, progress: {phase: "prepare", completed_bytes: 0, total_bytes_known: false}, members: [], runtimeMode: "image", failure: availabilityFailure(error instanceof Error ? error.message : error, "Availability status could not be loaded.")}}/><button type="button" className="button secondary" onClick={() => void load()}>Refresh availability status</button></div>}
    {!presentation && !loading && !error && <button type="button" className="button" onClick={() => void start(false)}>Make available</button>}
    {presentation && <LibraryAvailabilityOperation operation={presentation} modelAccessUrl={modelAccessUrl} onCheckAccessAndResume={member => void checkAccess(member)} onForce={() => void start(true)} onMakeAvailable={() => void start(false)} onRetry={() => void retry()}/>} 
  </section>;
}

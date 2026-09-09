import {useEffect, useMemo, useState} from "react";
import type {ControlApi, LibraryRecipeDetail, RecipeImageAvailabilityOperation} from "../api/types";
import {LibraryRequestError} from "./library-request-error";
import {LibraryAvailabilityOperation, recipeAvailabilityPresentation, selectAvailabilityOperation, type AvailabilityMemberPresentation} from "./library-availability-operation";

const terminal = new Set(["succeeded", "failed", "cancelled"]);

export function LibraryRecipeAvailability({api, detail, onBusyChange}: {api: ControlApi; detail: LibraryRecipeDetail; onBusyChange?(busy: boolean): void}) {
  const revisionId = detail.recipe.recipe_revision_id;
  const [operation, setOperation] = useState<RecipeImageAvailabilityOperation>();
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const modelAccessUrl = detail.model_documents[0]?.model_document.provenance.source_url;
  const activeChildren = operation?.children?.some(child => !terminal.has(child.state)) ?? false;
  const shouldPoll = Boolean(operation && (!terminal.has(operation.state) || activeChildren));
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
    onBusyChange?.(shouldPoll);
    return () => onBusyChange?.(false);
  }, [onBusyChange, shouldPoll]);

  useEffect(() => {
    if (!operation || !shouldPoll || !api.recipeAvailabilityOperation) return;
    const controller = new AbortController();
    let timer: number;
    async function poll() {
      try {
        const updated = await api.recipeAvailabilityOperation(operation!.id, controller.signal);
        if (!controller.signal.aborted) {
          setOperation(updated);
          setError(undefined);
        }
      } catch (value) {
        if (!controller.signal.aborted) {
          setError(value);
          timer = window.setTimeout(() => void poll(), 1_200);
        }
      }
    }
    timer = window.setTimeout(() => void poll(), 1_200);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [api, operation, shouldPoll]);

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
    {Boolean(error) && <LibraryRequestError error={error} title="Availability status could not be loaded." onRetry={() => void load()} retryLabel="Refresh availability status"/>}
    {!presentation && !loading && !error && <button type="button" className="button" onClick={() => void start(false)}>Make available</button>}
    {operation && terminal.has(operation.state) && activeChildren && <p role="status">Preparation needs attention. Remaining downloads and image preparation continue below; progress updates automatically.</p>}
    {presentation && <LibraryAvailabilityOperation operation={presentation} modelAccessUrl={modelAccessUrl} onCheckAccessAndResume={member => void checkAccess(member)} onForce={() => void start(true)} onMakeAvailable={() => void start(false)} onRetry={() => void retry()}/>} 
  </section>;
}

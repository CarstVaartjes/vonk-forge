import { useCallback, useEffect, useState, type FormEvent } from "react";
import type {
  ControlApi,
  ExternalModelList,
  ExternalModelPreviewRequest,
  ExternalModelReview,
} from "../api/types";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { StatusPill } from "../components/ui/status-pill";
import { Time } from "../components/ui/time";
import { useToast } from "../components/ui/toast";
import { ExternalModelAction } from "../api/vocabulary.generated";
import { safeErrorText } from "../lib/error-display";

type ExternalModel = ExternalModelList["models"][number];
type Pending = { alias: string; request: ExternalModelPreviewRequest; review: ExternalModelReview };

function failure(error: unknown, fallback: string): string {
  return safeErrorText(error instanceof Error ? error.message : fallback);
}

/** The Controller's own words for a review: its typed effects and the gateway's grace. */
function consequence(review: ExternalModelReview): string {
  const parts = [
    review.destination ? `Destination: ${review.destination.origins.join(", ")}.` : "",
    review.changed_from ? `Moves from: ${review.changed_from.origins.join(", ")}.` : "",
    review.credential_bindings?.length
      ? `Credentials: ${review.credential_bindings.join(", ")}.`
      : "",
    `Effects: ${review.effects.join(", ")}.`,
    `Streams in flight are cut after ${String(review.grace_seconds)} s.`,
    `If the gateway does not start with this entry, local models are down for at most ${String(review.failed_start_dark_seconds)} s.`,
  ];
  return parts.filter(Boolean).join(" ");
}

/** External models: published through the same gateway and client keys as the local ones. */
export function ExternalModelsSection({ api }: { api: ControlApi }) {
  const [models, setModels] = useState<ExternalModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [entry, setEntry] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setModels((await api.gatewayModels(signal)).models);
        setError(null);
      } catch (cause) {
        if (!signal?.aborted) setError(failure(cause, "Could not read external models"));
      }
    },
    [api],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function review(alias: string, request: ExternalModelPreviewRequest) {
    setBusy(true);
    setFormError(null);
    try {
      const reviewed = await api.previewGatewayModel(alias, request);
      if (reviewed.problems.length > 0) {
        setFormError(
          `Refused: ${reviewed.problems.map((problem) => `${problem.path} (${problem.code})`).join(", ")}`,
        );
        return;
      }
      setPending({ alias, request, review: reviewed });
    } catch (cause) {
      setFormError(failure(cause, "Could not review the change"));
    } finally {
      setBusy(false);
    }
  }

  async function put(event: FormEvent) {
    event.preventDefault();
    let parsed: unknown;
    try {
      parsed = JSON.parse(entry);
    } catch {
      setFormError("The entry is not valid JSON");
      return;
    }
    const alias =
      typeof parsed === "object" && parsed !== null && "model_name" in parsed
        ? String(parsed.model_name)
        : "";
    if (!alias) {
      setFormError("The entry needs a model_name: the alias clients call");
      return;
    }
    const existing = models?.find((model) => model.alias === alias);
    await review(alias, {
      action: ExternalModelAction.PUT,
      entry: parsed as NonNullable<ExternalModelPreviewRequest["entry"]>,
      ...(existing ? { expected_revision: existing.desired_revision } : {}),
    });
  }

  async function accept() {
    if (!pending) return;
    setBusy(true);
    try {
      await api.acceptGatewayModel(pending.alias, pending.request, pending.review);
      toast.success(`${pending.alias}: change accepted.`);
      setEntry("");
      await load();
    } catch (cause) {
      toast.error(failure(cause, "Could not accept the change"));
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  return (
    <section className="fleet-compact" aria-labelledby="external-models-heading">
      <h2 id="external-models-heading">External models</h2>
      <p>
        Models a partner or cloud provider serves, called with the same keys as the local models.
        CLI: <code>vonkctl gateway model put --file ENTRY.json</code>
      </p>
      {error && (
        <p role="alert" className="form-error">
          {error}
        </p>
      )}
      {models?.length === 0 && <p>No external models.</p>}
      {models && models.length > 0 && (
        <div className="fleet-table-scroll">
          <table>
            <caption className="sr-only">External models</caption>
            <thead>
              <tr>
                <th scope="col">Alias</th>
                <th scope="col">State</th>
                <th scope="col">Destination</th>
                <th scope="col">Revision</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {models.map((model) => (
                <tr key={model.alias}>
                  <th scope="row">{model.alias}</th>
                  <td>
                    <StatusPill tone={model.published ? "ok" : model.reason ? "bad" : "neutral"}>
                      {model.state}
                    </StatusPill>{" "}
                    {model.reason ?? ""}
                    {model.start_failures ? ` · failed starts ${String(model.start_failures)}` : ""}
                    {model.operator_action
                      ? ` · waiting for you: ${model.operator_action} (edit the entry or ${model.operator_action} it)`
                      : model.next_check_at
                        ? " · next check "
                        : ""}
                    {!model.operator_action && model.next_check_at && (
                      <Time value={model.next_check_at} />
                    )}
                  </td>
                  <td>{model.destination?.origins.join(", ") ?? "Unreadable entry"}</td>
                  <td>{String(model.desired_revision)}</td>
                  <td>
                    <div className="button-row">
                      {(!model.enabled || model.operator_action) && (
                        <button
                          type="button"
                          className="button secondary"
                          disabled={busy}
                          onClick={() =>
                            void review(model.alias, {
                              action: ExternalModelAction.ENABLE,
                              expected_revision: model.desired_revision,
                            })
                          }
                        >
                          {model.operator_action ? "Resume" : "Enable"}
                          <span className="sr-only"> {model.alias}</span>
                        </button>
                      )}
                      {model.enabled && (
                        <button
                          type="button"
                          className="button secondary"
                          disabled={busy}
                          onClick={() =>
                            void review(model.alias, {
                              action: ExternalModelAction.DISABLE,
                              expected_revision: model.desired_revision,
                            })
                          }
                        >
                          Disable<span className="sr-only"> {model.alias}</span>
                        </button>
                      )}
                      <button
                        type="button"
                        className="button secondary"
                        disabled={busy}
                        onClick={() =>
                          void review(model.alias, {
                            action: ExternalModelAction.REMOVE,
                            expected_revision: model.desired_revision,
                          })
                        }
                      >
                        Remove<span className="sr-only"> {model.alias}</span>
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form onSubmit={(event) => void put(event)}>
        <label htmlFor="external-entry">Model entry (one LiteLLM model_list entry, JSON)</label>
        <textarea
          id="external-entry"
          rows={8}
          spellCheck={false}
          value={entry}
          onChange={(event) => setEntry(event.currentTarget.value)}
        />
        <small className="field-hint">
          Name the credential as <code>os.environ/VONK_CREDENTIAL_NAME</code>; never paste a secret.
        </small>
        {formError && (
          <p role="alert" className="form-error">
            {formError}
          </p>
        )}
        <button type="submit" className="button" disabled={busy || !entry.trim()}>
          Review model
        </button>
      </form>
      {pending && (
        <ConfirmDialog
          title={`${pending.request.action} ${pending.alias}?`}
          consequence={consequence(pending.review)}
          confirmLabel="Accept change"
          command={`vonkctl gateway model ${pending.request.action} ${pending.request.action === ExternalModelAction.PUT ? "--file ENTRY.json" : pending.alias}`}
          busy={busy}
          onConfirm={() => void accept()}
          onCancel={() => setPending(null)}
        />
      )}
    </section>
  );
}

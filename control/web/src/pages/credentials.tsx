import { useCallback, useEffect, useState, type FormEvent } from "react";
import type {
  ControlApi,
  ExternalBindingList,
  ExternalBindingPreviewRequest,
  ExternalBindingReview,
} from "../api/types";
import { ConfirmDialog } from "../components/ui/confirm-dialog";
import { useToast } from "../components/ui/toast";
import { ExternalBindingAction } from "../api/vocabulary.generated";
import { safeErrorText } from "../lib/error-display";

type Binding = ExternalBindingList["bindings"][number];
type Pending = {
  name: string;
  request: ExternalBindingPreviewRequest;
  review: ExternalBindingReview;
};

function failure(error: unknown, fallback: string): string {
  return safeErrorText(error instanceof Error ? error.message : fallback);
}

/** Credentials: a name and where its secret may be sent. The secret is never entered here. */
export function CredentialsSection({ api }: { api: ControlApi }) {
  const [bindings, setBindings] = useState<Binding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [origins, setOrigins] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setBindings((await api.gatewayCredentials(signal)).bindings);
        setError(null);
      } catch (cause) {
        if (!signal?.aborted) setError(failure(cause, "Could not read credentials"));
      }
    },
    [api],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function review(bindingName: string, request: ExternalBindingPreviewRequest) {
    setBusy(true);
    setFormError(null);
    try {
      setPending({
        name: bindingName,
        request,
        review: await api.previewGatewayCredential(bindingName, request),
      });
    } catch (cause) {
      setFormError(failure(cause, "Could not review the change"));
    } finally {
      setBusy(false);
    }
  }

  async function put(event: FormEvent) {
    event.preventDefault();
    const existing = bindings?.find((binding) => binding.name === name.trim());
    await review(name.trim(), {
      action: ExternalBindingAction.PUT,
      destinations: origins
        .split(/[\s,]+/)
        .filter(Boolean)
        .map((origin) => ({ origin })),
      ...(existing ? { expected_revision: existing.revision } : {}),
    });
  }

  async function accept() {
    if (!pending) return;
    setBusy(true);
    try {
      await api.acceptGatewayCredential(pending.name, pending.request, pending.review);
      toast.success(`Credential ${pending.name}: change accepted.`);
      setName("");
      setOrigins("");
      await load();
    } catch (cause) {
      toast.error(failure(cause, "Could not accept the change"));
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  const withdrawn = pending?.review.dependents.filter((dependent) => dependent.withdrawn) ?? [];
  return (
    <section className="fleet-compact" aria-labelledby="credentials-heading">
      <h2 id="credentials-heading">Credentials</h2>
      <p>
        A credential is a name; its secret is provisioned on the NAS and never entered here. CLI:{" "}
        <code>vonkctl gateway credential put NAME --origin URL</code>
      </p>
      {error && (
        <p role="alert" className="form-error">
          {error}
        </p>
      )}
      {bindings?.length === 0 && <p>No credentials.</p>}
      {bindings && bindings.length > 0 && (
        <div className="fleet-table-scroll">
          <table>
            <caption className="sr-only">Credentials and where each may be sent</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Secret loaded</th>
                <th scope="col">Approved destinations</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {bindings.map((binding) => (
                <tr key={binding.name}>
                  <th scope="row">{binding.name}</th>
                  <td>
                    {binding.credential_loaded == null
                      ? "Not reported"
                      : binding.credential_loaded
                        ? "Yes"
                        : "No"}
                  </td>
                  <td>
                    {binding.destinations.map((destination) => destination.origin).join(", ")}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="button secondary"
                      disabled={busy}
                      onClick={() =>
                        void review(binding.name, {
                          action: ExternalBindingAction.REMOVE,
                          expected_revision: binding.revision,
                        })
                      }
                    >
                      Remove<span className="sr-only"> {binding.name}</span>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <form onSubmit={(event) => void put(event)}>
        <label htmlFor="credential-name">Credential name</label>
        <input
          id="credential-name"
          required
          pattern="[A-Z][A-Z0-9_]*"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
          placeholder="PARTNER"
        />
        <label htmlFor="credential-origins">Approved origins (https, separated by spaces)</label>
        <input
          id="credential-origins"
          required
          value={origins}
          onChange={(event) => setOrigins(event.currentTarget.value)}
          placeholder="https://api.partner.example"
        />
        {formError && (
          <p role="alert" className="form-error">
            {formError}
          </p>
        )}
        <button type="submit" className="button" disabled={busy}>
          Review credential
        </button>
      </form>
      {pending && (
        <ConfirmDialog
          title={`${pending.request.action} credential ${pending.name}?`}
          consequence={
            withdrawn.length > 0
              ? `Withdraws: ${withdrawn.map((dependent) => dependent.alias).join(", ")}.`
              : "No model is withdrawn."
          }
          confirmLabel="Accept change"
          command={`vonkctl gateway credential ${pending.request.action} ${pending.name}`}
          busy={busy}
          onConfirm={() => void accept()}
          onCancel={() => setPending(null)}
        />
      )}
    </section>
  );
}

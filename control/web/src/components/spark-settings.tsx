import { useState } from "react";
import type { ControlApi, EnrollmentGrantResponse, VisualFleetNode } from "../api/types";
import { errorText } from "../lib/error-display";
import { ConfirmDialog } from "./confirm-dialog";
import { EnrollmentGrant } from "./enrollment-grant";
import { useToast } from "./toast";

enum Dialog {
  Remove,
  Reenroll,
}

/** The Settings tab: rename, re-enroll and remove. `onChanged` tells the page to re-read the fleet. */
export function SparkSettings({
  api,
  node,
  onChanged,
  onRemoved,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  onChanged(): void;
  onRemoved(): void;
}) {
  const id = node.id;
  const [grant, setGrant] = useState<EnrollmentGrantResponse | null>(null);
  const [action, setAction] = useState<Dialog | null>(null);
  const [name, setName] = useState(node.display_name);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  async function run(work: () => Promise<string>, fallback: string) {
    setBusy(true);
    try {
      toast.success(await work());
    } catch (cause) {
      toast.error(errorText(cause, fallback));
    } finally {
      setBusy(false);
      setAction(null);
    }
  }
  const rename = () =>
    run(async () => {
      const identity = await api.renameFleetNode(id, name.trim());
      onChanged();
      return `Spark renamed to ${identity.display_name}.`;
    }, "Could not rename this Spark");
  const remove = () =>
    run(async () => {
      await api.removeFleetNode(id);
      onRemoved();
      return "Spark removed.";
    }, "Could not remove this Spark");
  const reenroll = () =>
    run(async () => {
      const response = await api.reenrollFleetNode(id, crypto.randomUUID());
      if (!response.grant) throw new Error("Controller did not return an enrollment grant");
      setGrant(response.grant);
      return "Re-enrollment grant created.";
    }, "Could not create a re-enrollment grant");

  return (
    <>
      <form
        className="settings-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim()) void rename();
        }}
      >
        <label>
          Spark name
          <input
            required
            maxLength={80}
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
          />
        </label>
        <div className="button-row">
          <button
            type="submit"
            className="button"
            disabled={busy || !name.trim() || name.trim() === node.display_name}
          >
            Save name
          </button>
        </div>
        <p>
          CLI: <code>vonkctl fleet rename {id} &lt;name&gt;</code>
        </p>
      </form>
      <section className="danger-zone" aria-labelledby="spark-danger-heading">
        <h3 id="spark-danger-heading">Danger zone</h3>
        <div className="button-row">
          <button
            type="button"
            className="button secondary"
            onClick={() => setAction(Dialog.Reenroll)}
          >
            Re-enroll
          </button>
          <button type="button" className="button danger" onClick={() => setAction(Dialog.Remove)}>
            Remove
          </button>
        </div>
        {action === Dialog.Reenroll && (
          <ConfirmDialog
            title="Re-enroll this Spark?"
            consequence="Its current agent credentials are replaced. The Spark keeps its name and recipes but must run the new installer command to reconnect."
            confirmLabel="Create re-enrollment grant"
            command={`vonkctl fleet re-enroll ${id} --yes`}
            busy={busy}
            onConfirm={() => void reenroll()}
            onCancel={() => setAction(null)}
          />
        )}
        {action === Dialog.Remove && (
          <ConfirmDialog
            title="Remove this Spark?"
            consequence="It leaves the fleet and its agent credentials are revoked. Enrolling it again needs a new grant. This cannot be undone."
            confirmLabel="Remove Spark"
            typeName={node.display_name}
            command={`vonkctl fleet remove ${id} --yes`}
            busy={busy}
            onConfirm={() => void remove()}
            onCancel={() => setAction(null)}
          />
        )}
        {grant && <EnrollmentGrant api={api} grant={grant} />}
      </section>
    </>
  );
}

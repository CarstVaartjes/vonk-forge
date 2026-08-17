import {useEffect, useId, useState} from "react";
import type {ChangeEvent} from "react";
import type {LibraryRecipeDetail} from "../api/types";
import {parseVisualRecipeDocument} from "../lib/library-recipe-document";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;

export function LibraryRecipeAdvanced({document, onValidDocument, resetToken}: {
  document: VisualRecipeDocument;
  onValidDocument(document: VisualRecipeDocument): void;
  resetToken: string;
}) {
  const [text, setText] = useState(() => JSON.stringify(document, null, 2));
  const [error, setError] = useState("");
  const errorId = useId();

  useEffect(() => {
    setText(JSON.stringify(document, null, 2));
    setError("");
  }, [resetToken]);

  function preview(nextText: string) {
    setText(nextText);
    const result = parseVisualRecipeDocument(nextText);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setError("");
    onValidDocument(result.document);
  }

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (!file) return;
    try {
      preview(await file.text());
    } catch {
      setError("Unable to read the selected JSON file.");
    } finally {
      input.value = "";
    }
  }

  return <details className="library-advanced" aria-label="Advanced recipe document">
    <summary>Advanced recipe document</summary>
    <div className="library-advanced-content">
      <p>Edit or upload canonical visual JSON for a local preview. This does not apply, save, resolve, or publish the recipe.</p>
      <label htmlFor={`${errorId}-editor`}>Recipe JSON</label>
      <textarea
        id={`${errorId}-editor`}
        rows={18}
        spellCheck={false}
        value={text}
        aria-describedby={error ? errorId : undefined}
        aria-invalid={error ? "true" : "false"}
        onChange={event => preview(event.currentTarget.value)}
      />
      <label className="library-upload" htmlFor={`${errorId}-upload`}>Upload recipe JSON
        <input
          id={`${errorId}-upload`}
          type="file"
          accept="application/json,.json"
          aria-describedby={error ? errorId : undefined}
          aria-invalid={error ? "true" : "false"}
          onChange={event => void upload(event)}
        />
      </label>
      {error && <p id={errorId} className="library-document-error" role="alert">{error}</p>}
    </div>
  </details>;
}

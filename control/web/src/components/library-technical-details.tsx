import {useId, useState} from "react";

export type TechnicalValue = {
  label: string;
  value: string;
};

export function humanizeIdentifier(value: string): string {
  const normalized = value
    .replace(/^spk[_-]?/i, "spark ")
    .replace(/[_./-]+/g, " ")
    .replace(/([a-z])([0-9])/gi, "$1 $2")
    .replace(/([0-9])([a-z])/gi, "$1 $2")
    .trim();
  const preferred: Record<string, string> = {api: "API", bf: "BF", cuda: "CUDA", fp: "FP", gguf: "GGUF", glm: "GLM", gpt: "GPT", gpu: "GPU", llm: "LLM", mia: "MIA", nccl: "NCCL", openai: "OpenAI", qwen: "Qwen", ui: "UI", ux: "UX", vllm: "vLLM"};
  return normalized.split(/\s+/).map(part => {
    if (preferred[part.toLocaleLowerCase()]) return preferred[part.toLocaleLowerCase()];
    return part.charAt(0).toUpperCase() + part.slice(1);
  }).join(" ");
}

export function friendlyModelName(model: {publisher: string; slug: string}): string {
  const publisher = humanizeIdentifier(model.publisher);
  const slug = humanizeIdentifier(model.slug);
  return slug.toLocaleLowerCase().startsWith(publisher.toLocaleLowerCase()) ? slug : `${publisher} ${slug}`;
}

export function exactIdentity(value: {publisher: string; slug: string; content_sha256: string}): string {
  return `${value.publisher}/${value.slug}@${value.content_sha256}`;
}

export function TechnicalDetails({items, label = "Technical details", compact = false}: {
  items: readonly TechnicalValue[];
  label?: string;
  compact?: boolean;
}) {
  const [copied, setCopied] = useState("");
  const [open, setOpen] = useState(false);
  const statusId = useId();
  const visibleItems = items.filter(item => item.value.trim().length > 0);
  if (visibleItems.length === 0) return null;

  async function copy(item: TechnicalValue) {
    try {
      await navigator.clipboard?.writeText(item.value);
      setCopied(`${item.label} copied`);
    } catch {
      setCopied(`Could not copy ${item.label.toLocaleLowerCase()}`);
    }
  }

  return <details className={`technical-details${compact ? " technical-details-compact" : ""}`} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 7.5h16M4 12h16M4 16.5h10" strokeLinecap="round"/></svg>{label}<svg className="technical-details-chevron" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m6 9 6 6 6-6" strokeLinecap="round" strokeLinejoin="round"/></svg></summary>
    {open && <><dl>
      {visibleItems.map(item => <div key={`${item.label}:${item.value}`}>
        <dt>{item.label}</dt>
        <dd><code>{item.value}</code><button type="button" className="technical-copy" aria-describedby={statusId} onClick={() => void copy(item)}>Copy <span className="visually-hidden">{item.label}</span></button></dd>
      </div>)}
    </dl>
    <span className="visually-hidden" id={statusId} role="status" aria-live="polite">{copied}</span></>}
  </details>;
}

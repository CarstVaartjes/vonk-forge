import { CopyButton } from "./copy-button";

// The CLI renders the same text (src/cluster_profiles/cli_render/profile.py);
// tests/fixtures/connection-examples.json is asserted by both renderers.
const PER_REQUEST_NOTE =
  "Thinking and sampling are usually set per request (for example chat_template_kwargs and temperature), not in the recipe.";

function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

export function connectionSnippets(apiBase: string, alias: string) {
  const body = JSON.stringify({ model: alias, messages: [{ role: "user", content: "Hello" }] });
  return {
    setup: [
      "vonkctl key create NAME --output CLIENT_KEY_FILE",
      'export OPENAI_API_KEY="$(cat CLIENT_KEY_FILE)"',
      `export OPENAI_BASE_URL=${shellQuote(apiBase.replace(/\/+$/, ""))}`,
    ].join("\n"),
    curl: [
      'curl "$OPENAI_BASE_URL/chat/completions" \\',
      '  -H "Authorization: Bearer $OPENAI_API_KEY" \\',
      '  -H "Content-Type: application/json" \\',
      `  -d ${shellQuote(body)}`,
    ].join("\n"),
    python: [
      "from openai import OpenAI",
      "",
      "client = OpenAI()",
      "response = client.chat.completions.create(",
      `    model=${JSON.stringify(alias)},`,
      `    messages=[{"role": "user", "content": "Hello"}],`,
      ")",
      "print(response.choices[0].message.content)",
    ].join("\n"),
  };
}

export { PER_REQUEST_NOTE };

export function ConnectionExamples({ apiBase, alias }: { apiBase: string; alias: string }) {
  const snippets = connectionSnippets(apiBase, alias);
  const examples = [
    { label: "setup example", code: snippets.setup },
    { label: "curl example", code: snippets.curl },
    { label: "Python example", code: snippets.python },
  ];
  return (
    <div className="connection-examples">
      {examples.map(({ label, code }) => (
        <div key={label}>
          <pre aria-label={label}>
            <code>{code}</code>
          </pre>
          <CopyButton label={label} value={code} />
        </div>
      ))}
      <p>{PER_REQUEST_NOTE}</p>
    </div>
  );
}

import { CopyButton } from "./copy-button";

const PER_REQUEST_NOTE =
  "Thinking and sampling are set per request (for example chat_template_kwargs and temperature), not in the recipe.";

export function curlExample(apiBase: string, alias: string): string {
  return [
    `curl ${apiBase.replace(/\/+$/, "")}/chat/completions \\`,
    `  -H "Authorization: Bearer $VONK_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '${JSON.stringify({ model: alias, messages: [{ role: "user", content: "Hello" }] })}'`,
  ].join("\n");
}

export function pythonExample(apiBase: string, alias: string): string {
  return [
    "import os",
    "from openai import OpenAI",
    "",
    `client = OpenAI(base_url=${JSON.stringify(apiBase)}, api_key=os.environ["VONK_API_KEY"])`,
    "response = client.chat.completions.create(",
    `    model=${JSON.stringify(alias)},`,
    `    messages=[{"role": "user", "content": "Hello"}],`,
    ")",
    "print(response.choices[0].message.content)",
  ].join("\n");
}

export function ConnectionExamples({ apiBase, alias }: { apiBase: string; alias: string }) {
  const examples = [
    { label: "curl example", code: curlExample(apiBase, alias) },
    { label: "Python example", code: pythonExample(apiBase, alias) },
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

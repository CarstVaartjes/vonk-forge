/** Bound untrusted error text and remove credentials before rendering it. */
export function safeErrorText(value: string, maximum = 512): string {
  const text = value
    .replace(/\u0000/g, "")
    .replace(/(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+/gim, "$1<redacted>")
    .replace(/(token|api[_-]?key|secret|password)\s*[:=]\s*[^\s,;]+/gim, "$1=<redacted>")
    .replace(/https?:\/\/[^\s?]+\?[^\s]+/g, "<signed-url-redacted>")
    .trim();
  return text.length > maximum ? `${text.slice(0, maximum - 15)}…<truncated>` : text;
}

/** Failure notice text that always names the request, so support can find the call. */
export function failureNotice(message: string, requestId: string): string {
  const text = safeErrorText(message, 256);
  return /request ID/i.test(text) ? text : `${text} (request ID ${requestId})`;
}

/** Safe text for a caught value: its message when it is an Error, else the fallback. */
export function errorText(error: unknown, fallback: string): string {
  return safeErrorText(error instanceof Error ? error.message : fallback);
}

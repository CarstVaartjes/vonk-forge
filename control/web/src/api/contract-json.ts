import * as generated from "./runtime.generated.js";
import {parseContractJson, stringifyContractJson} from "./contract-numeric";

export class ContractViolation extends Error {
  readonly path: string;
  constructor(readonly method: string, path: string, readonly status: number) {
    const pathname = new URL(path, "http://control.invalid").pathname;
    super(`Invalid Control API contract: ${method} ${pathname} (${status})`);
    this.path = pathname;
    this.name = "ContractViolation";
  }
}
function routeFor(method: string, path: string) {
  const pathname = new URL(path, "http://control.invalid").pathname;
  return generated.contractRoutes.find(route => route.method === method.toUpperCase() &&
    new RegExp(`^${route.route.split(/(\{[^}]+\})/).map(part => part.startsWith("{") ? "[^/]+" : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("")}$`).test(pathname));
}
export function validateControlBody(method: string, path: string, status: number, media: string, text: string): unknown {
  const route = routeFor(method, path);
  const responses = route?.responses[String(status)] ?? route?.responses[`${Math.floor(status / 100)}XX`] ?? route?.responses.default;
  if (responses && Object.keys(responses).length === 0 && text === "") return undefined;
  const validator = responses?.[media.split(";")[0].trim().toLowerCase()];
  if (!validator) throw new ContractViolation(method, path, status);
  let value: unknown;
  try { value = parseContractJson(text); } catch { throw new ContractViolation(method, path, status); }
  if (!validator(value)) throw new ContractViolation(method, path, status);
  return validator.normalize(value);
}
export function validateComponent(name: string, text: string): unknown {
  const validator = generated[`component${name}` as keyof typeof generated];
  const value = parseContractJson(text);
  if (typeof validator !== "function" || !validator(value)) throw new ContractViolation("EVENT", name, 0);
  return validator.normalize(value);
}
export function serializeControlBody(method: string, path: string, value: unknown): string {
  const route = routeFor(method, path);
  const validator = route?.requests["application/json"];
  const text = stringifyContractJson(value);
  if (!validator || !validator(parseContractJson(text))) throw new ContractViolation(method, path, 0);
  return text;
}
export function validateControlParameters(method: string, path: string, parameters: {path?: Record<string, unknown>; query?: Record<string, unknown>}): void {
  const route = routeFor(method, path);
  if (!route) throw new ContractViolation(method, path, 0);
  for (const location of ["path", "query"] as const) {
    // openapi-fetch's optional query arguments intentionally omit undefined.
    const values = Object.fromEntries(Object.entries(parameters[location] ?? {}).filter(([, value]) => value !== undefined));
    const validator = route.parameters[location];
    if (validator ? !validator(parseContractJson(stringifyContractJson(values))) : Object.keys(values).length !== 0) throw new ContractViolation(method, path, 0);
  }
}
export class ContractResponseTooLarge extends ContractViolation {
  constructor(method: string, path: string, status: number,
    readonly budgetBytes: number, readonly observedBytes: string) {
    super(method, path, status);
    this.name = "ContractResponseTooLarge";
    this.message = `Control API response exceeds its producer byte budget: ${method} ${this.path} (limit ${budgetBytes} bytes)`;
  }
}
/** Check actual wire bytes before decoding or retaining each body chunk. */
export async function readControlResponseText(response: Response, method: string, path: string): Promise<string> {
  const budget = routeFor(method, path)?.responseMaxBytes;
  if (budget === undefined) return response.text();
  const reader = response.body?.getReader();
  let received = 0;
  const chunks: string[] = [];
  const decoder = new TextDecoder();
  try {
    const declared = response.headers.get("content-length");
    if (declared !== null && /^[0-9]+$/.test(declared) && BigInt(declared) > BigInt(budget)) {
      throw new ContractResponseTooLarge(method, path, response.status, budget, declared);
    }
    if (!reader) return "";
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      if (next.value.byteLength > budget - received) {
        throw new ContractResponseTooLarge(method, path, response.status, budget,
          (BigInt(received) + BigInt(next.value.byteLength)).toString());
      }
      received += next.value.byteLength;
      chunks.push(decoder.decode(next.value, {stream: true}));
    }
    chunks.push(decoder.decode());
    return chunks.join("");
  } catch (cause) {
    await reader?.cancel().catch(() => undefined);
    throw cause;
  } finally {
    reader?.releaseLock();
  }
}
/** Consume once; only actual producer-owned route budgets apply. */
export async function readControlResponse(response: Response, method: string, path: string): Promise<unknown> {
  return validateControlBody(method, path, response.status, response.headers.get("content-type") ?? "", await readControlResponseText(response, method, path));
}
/** openapi-fetch must consume the validated value instead of calling JSON.parse. */
export class ContractResponse extends Response {
  constructor(response: Response, private readonly value: unknown, private readonly sourceText: string) {
    super(sourceText, {status: response.status, statusText: response.statusText, headers: response.headers});
    for (const key of ["url", "redirected", "type"] as const) Object.defineProperty(this, key, {value: response[key]});
  }
  override async json(): Promise<unknown> { await this.text(); return this.value; }
  override clone(): Response {
    return new ContractResponse(super.clone(), this.value, this.sourceText);
  }
}

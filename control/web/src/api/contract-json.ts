import * as generated from "./runtime.generated.js";
import {materialize, parseContractJson, stringifyContractJson} from "./contract-numeric";

export class ContractViolation extends Error {
  constructor(readonly method: string, readonly path: string, readonly status: number) {
    super(`Invalid Control API contract: ${method} ${path} (${status})`);
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
  const validator = responses?.[media.split(";")[0].trim().toLowerCase()];
  if (!validator) throw new ContractViolation(method, path, status);
  let value: unknown;
  try { value = parseContractJson(text); } catch { throw new ContractViolation(method, path, status); }
  if (!validator(value)) throw new ContractViolation(method, path, status);
  return materialize(value);
}
export function validateComponent(name: string, text: string): unknown {
  const validator = generated[`component${name}` as keyof typeof generated];
  const value = parseContractJson(text);
  if (typeof validator !== "function" || !validator(value)) throw new ContractViolation("EVENT", name, 0);
  return materialize(value);
}
export function serializeControlBody(method: string, path: string, value: unknown): string {
  const route = routeFor(method, path);
  const validator = route?.requests["application/json"];
  const text = stringifyContractJson(value);
  if (!validator || !validator(parseContractJson(text))) throw new ContractViolation(method, path, 0);
  return text;
}
/** Consume once; route owners, rather than the parser, determine byte budgets. */
export async function readControlResponse(response: Response, method: string, path: string): Promise<unknown> {
  return validateControlBody(method, path, response.status, response.headers.get("content-type") ?? "", await response.text());
}
/** openapi-fetch must consume the validated value instead of calling JSON.parse. */
export class ContractResponse extends Response {
  constructor(response: Response, private readonly value: unknown, text: string) {
    super(text, {status: response.status, statusText: response.statusText, headers: response.headers});
    for (const key of ["url", "redirected", "type"] as const) Object.defineProperty(this, key, {value: response[key]});
  }
  override async json(): Promise<unknown> { await this.text(); return this.value; }
  override clone(): Response {
    const response = super.clone();
    Object.defineProperty(response, "json", {value: async () => { await response.text(); return this.value; }});
    return response;
  }
}

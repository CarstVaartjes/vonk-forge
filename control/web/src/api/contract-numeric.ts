import {LosslessNumber} from "lossless-json";

/** The raw numeric token is retained until its canonical schema accepts it. */
export type ExactNumber = LosslessNumber;
export type ExactInteger = ExactNumber;
export type WireNumber = number | ExactNumber;

function unsignedCompare(a: string, b: string): number {
  a = a.replace(/^0+/, "") || "0"; b = b.replace(/^0+/, "") || "0";
  return a.length === b.length ? (a === b ? 0 : a < b ? -1 : 1) : a.length < b.length ? -1 : 1;
}
function unsignedAdd(a: string, b: string): string {
  let carry = 0; const digits: string[] = [];
  for (let i = a.length - 1, j = b.length - 1; i >= 0 || j >= 0 || carry; i--, j--) {
    const digit = Number(a[i] ?? 0) + Number(b[j] ?? 0) + carry;
    digits.push(String(digit % 10)); carry = Math.floor(digit / 10);
  }
  return digits.reverse().join("");
}
function unsignedSubtract(a: string, b: string): string {
  const digits: string[] = []; let borrow = 0;
  for (let i = a.length - 1, j = b.length - 1; i >= 0; i--, j--) {
    let digit = Number(a[i]) - Number(b[j] ?? 0) - borrow;
    borrow = digit < 0 ? 1 : 0; if (borrow) digit += 10; digits.push(String(digit));
  }
  return digits.reverse().join("").replace(/^0+/, "") || "0";
}
function signed(value: string): {negative: boolean; digits: string} {
  const digits = value.replace(/^[+-]/, "").replace(/^0+/, "") || "0";
  return {negative: digits !== "0" && value.startsWith("-"), digits};
}
function addOffset(value: string, offset: number): string {
  return signedAdd(value, String(offset));
}
function signedAdd(left: string, right: string): string {
  const a = signed(left), b = signed(right);
  if (a.negative === b.negative) return (a.negative ? "-" : "") + unsignedAdd(a.digits, b.digits);
  const comparison = unsignedCompare(a.digits, b.digits);
  if (!comparison) return "0";
  const larger = comparison > 0 ? a : b, smaller = comparison > 0 ? b : a;
  return (larger.negative ? "-" : "") + unsignedSubtract(larger.digits, smaller.digits);
}
function signedCompare(a: string, b: string): number {
  const x = signed(a), y = signed(b);
  return x.negative !== y.negative ? x.negative ? -1 : 1 : unsignedCompare(x.digits, y.digits) * (x.negative ? -1 : 1);
}
function decimal(value: string) {
  const match = /^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/.exec(value);
  if (!match) throw new Error("Invalid numeric token");
  const digits = (match[2] + (match[3] ?? "")).replace(/^0+/, "");
  return {negative: !!match[1] && digits !== "", digits, magnitude: addOffset(match[4] ?? "0", digits.length - (match[3]?.length ?? 0))};
}
/** Linear in token bytes: an exponent is never expanded into a power of ten. */
export function compareNumeric(a: string, b: string): number {
  const x = decimal(a), y = decimal(b);
  if (!x.digits || !y.digits) return !x.digits && !y.digits ? 0 : !x.digits ? (y.negative ? 1 : -1) : x.negative ? -1 : 1;
  if (x.negative !== y.negative) return x.negative ? -1 : 1;
  let result = signedCompare(x.magnitude, y.magnitude);
  if (!result) {
    const length = Math.max(x.digits.length, y.digits.length);
    for (let i = 0; i < length; i++) {
      const left = x.digits[i] ?? "0", right = y.digits[i] ?? "0";
      if (left !== right) { result = left < right ? -1 : 1; break; }
    }
  }
  return result * (x.negative ? -1 : 1);
}
export function numericToken(value: unknown): string | undefined {
  if (value instanceof LosslessNumber) return value.value;
  return typeof value === "number" && Number.isFinite(value) ? String(value) : undefined;
}
export function isWireNumber(value: unknown): value is WireNumber { return numericToken(value) !== undefined; }
export function compareWire(a: WireNumber, b: WireNumber): number { return compareNumeric(numericToken(a)!, numericToken(b)!); }
export function formatWire(value: WireNumber): string { return numericToken(value)!; }
function integerResult(token: string): WireNumber {
  const number = Number(token); return Number.isSafeInteger(number) ? number : new LosslessNumber(token);
}
export function addWire(a: WireNumber, b: WireNumber): WireNumber {
  const left = numericToken(a)!, right = numericToken(b)!;
  if (/[.eE]/.test(left + right)) throw new Error("Exact integer arithmetic requires integer tokens");
  return integerResult(signedAdd(left, right));
}
export function subtractWire(a: WireNumber, b: WireNumber): WireNumber {
  const right = numericToken(b)!;
  return addWire(a, integerResult(right.startsWith("-") ? right.slice(1) : `-${right}`));
}
/** Explicit integer arithmetic; result digits are bounded by input digits. */
export function multiplyWire(a: WireNumber, b: WireNumber): WireNumber {
  const left = numericToken(a)!, right = numericToken(b)!;
  if (/[.eE]/.test(left + right)) throw new Error("Exact integer arithmetic requires integer tokens");
  // Conversion is on-demand arithmetic, never eager network parsing. It uses
  // only the provided integer digits and never expands an exponent token.
  return integerResult((BigInt(left) * BigInt(right)).toString());
}
/** Presentation only. Exact source values remain available for copy/export. */
export function displayRatio(a: WireNumber, b: WireNumber): number {
  const x = decimal(numericToken(a)!), y = decimal(numericToken(b)!);
  if (!y.digits) throw new Error("Ratio denominator is zero");
  if (!x.digits) return 0;
  const shiftText = signedAdd(x.magnitude, y.magnitude.startsWith("-") ? y.magnitude.slice(1) : `-${y.magnitude}`);
  const shift = signedCompare(shiftText, "309") > 0 ? 309 : signedCompare(shiftText, "-325") < 0 ? -325 : Number(shiftText);
  const coefficient = (digits: string) => Number(digits.slice(0, 16)) / 10 ** (Math.min(digits.length, 16) - 1);
  const ratio = coefficient(x.digits) / coefficient(y.digits) * 10 ** shift * (x.negative !== y.negative ? -1 : 1);
  // The mantissas are a bounded display approximation. Round away their
  // binary arithmetic noise; authority and exported values retain raw tokens.
  return Number.isFinite(ratio) ? Number(ratio.toPrecision(15)) : ratio;
}
export function contractType(value: unknown, types: string[], strictIntegerTokens = true, finiteNumbers = true): boolean {
  return types.some(type => {
    const token = numericToken(value);
    if (type === "integer") return token !== undefined && (strictIntegerTokens ? /^-?(?:0|[1-9][0-9]*)$/.test(token) : numericMultiple(value, "1"));
    if (type === "number") return token !== undefined && (!finiteNumbers || Number.isFinite(Number(token)));
    if (type === "object") return value !== null && typeof value === "object" && !Array.isArray(value) && !(value instanceof LosslessNumber);
    if (type === "array") return Array.isArray(value);
    if (type === "null") return value === null;
    return typeof value === type;
  });
}
export function numericBound(value: unknown, bound: string, operator: string, ieeeFloat = false): boolean {
  const token = numericToken(value); if (token === undefined) return true;
  const comparison = ieeeFloat
    ? (Number(token) === Number(bound) ? 0 : Number(token) < Number(bound) ? -1 : 1)
    : compareNumeric(token, bound);
  return operator === "minimum" ? comparison >= 0 : operator === "maximum" ? comparison <= 0 : operator === "exclusiveMinimum" ? comparison > 0 : comparison < 0;
}
export function contractEqual(value: unknown, expected: unknown): boolean {
  const a = numericToken(value), b = numericToken(expected);
  if (a !== undefined || b !== undefined) return a !== undefined && b !== undefined && compareNumeric(a, b) === 0;
  if (Array.isArray(value) || Array.isArray(expected)) return Array.isArray(value) && Array.isArray(expected) && value.length === expected.length && value.every((item, index) => contractEqual(item, expected[index]));
  if (value !== null && expected !== null && typeof value === "object" && typeof expected === "object") {
    const left = Object.keys(value), right = Object.keys(expected);
    return left.length === right.length && left.every(key => Object.hasOwn(expected, key) && contractEqual((value as Record<string, unknown>)[key], (expected as Record<string, unknown>)[key]));
  }
  return value === expected;
}
const expectedValues = new Map<string, unknown>();
export function contractEnum(value: unknown, expected: string[]): boolean {
  return expected.some(text => {
    if (!expectedValues.has(text)) expectedValues.set(text, parseContractJson(text));
    return contractEqual(value, expectedValues.get(text));
  });
}
export function numericMultiple(value: unknown, divisor: string): boolean {
  const token = numericToken(value); if (token === undefined) return true;
  const x = decimal(token), y = decimal(divisor); if (!x.digits) return true;
  // Only the trusted, compiled schema divisor becomes a BigInt. Network digits
  // are processed by streaming remainder, never by exponent-sized allocation.
  let coefficient = BigInt(y.digits), twos = 0, fives = 0;
  if (coefficient <= 0n) return false;
  while (coefficient % 2n === 0n) { coefficient /= 2n; twos++; }
  while (coefficient % 5n === 0n) { coefficient /= 5n; fives++; }
  const xExponent = addOffset(x.magnitude, -x.digits.length);
  const yExponent = addOffset(y.magnitude, -y.digits.length);
  const delta = signedAdd(xExponent, yExponent.startsWith("-") ? yExponent.slice(1) : `-${yExponent}`);
  // Negative decimal shifts may still be integral when numerator has factors
  // of ten; remove those token zeros without expanding either exponent.
  const trailing = x.digits.length - x.digits.replace(/0+$/, "").length;
  const adjusted = addOffset(delta, trailing);
  if (signedCompare(adjusted, "0") < 0) return false;
  const shift = signedCompare(adjusted, String(Math.max(twos, fives))) >= 0 ? Math.max(twos, fives) : Number(adjusted);
  const modulus = coefficient * 2n ** BigInt(Math.max(0, twos - shift)) * 5n ** BigInt(Math.max(0, fives - shift));
  let remainder = 0n;
  for (const digit of x.digits.slice(0, x.digits.length - trailing)) remainder = (remainder * 10n + BigInt(digit)) % modulus;
  return remainder === 0n;
}
// Ordinary Controller/CLI JSON ingress uses last-key wins. Duplicate rejection
// belongs to an explicitly stricter owning boundary, not this browser parser.
export class UnsupportedContractRuntime extends Error {
  constructor() {
    super("This browser cannot preserve JSON numeric source tokens. Update your browser to use the Control API.");
    this.name = "UnsupportedContractRuntime";
  }
}
export function parseContractJson(text: string): unknown {
  return JSON.parse(text, (_key: string, value: unknown, context?: {source?: string}) => {
    if (typeof value !== "number") return value;
    if (typeof context?.source !== "string") throw new UnsupportedContractRuntime();
    return new LosslessNumber(context.source);
  });
}
export interface NormalizationShape {
  type?: string | string[];
  preserveIntegerFloat?: boolean;
  ref?: string;
  alternatives?: {validate: (value: unknown) => boolean; shape: NormalizationShape}[];
  allOf?: NormalizationShape[];
  properties?: Record<string, NormalizationShape>;
  items?: NormalizationShape;
  additionalProperties?: NormalizationShape;
}
/** Projection uses the successful compiled schema branch, never a second validator. */
export function normalizeValidated(value: unknown, shape: NormalizationShape, definitions: Record<string, NormalizationShape>): unknown {
  function expand(current: NormalizationShape, seen: Set<string>): NormalizationShape[] {
    if (current.ref) {
      if (seen.has(current.ref)) return [];
      const referenced = definitions[current.ref];
      if (!referenced) throw new Error("Missing canonical normalization reference");
      return expand(referenced, new Set([...seen, current.ref]));
    }
    const alternative = current.alternatives?.find(item => item.validate(value));
    return [current, ...(alternative ? expand(alternative.shape, seen) : []), ...(current.allOf ?? []).flatMap(item => expand(item, seen))];
  }
  const shapes = expand(shape, new Set());
  if (value instanceof LosslessNumber) {
    const types = shapes.flatMap(item => typeof item.type === "string" ? [item.type] : item.type ?? []);
    if (types.includes("integer")) return materialize(value);
    if (types.includes("number")) {
      const number = Number(value.value);
      if (!Number.isFinite(number)) throw new Error("Non-finite contract number");
      if (Number.isInteger(number) && shapes.some(item => item.preserveIntegerFloat)) {
        const token = Object.is(number, -0) ? "-0.0" : String(number);
        return new LosslessNumber(/[.eE]/.test(token) ? token : `${token}.0`);
      }
      return number;
    }
    return materialize(value);
  }
  if (Array.isArray(value)) {
    const items = shapes.map(item => item.items).filter((item): item is NormalizationShape => item !== undefined);
    return value.map(item => normalizeValidated(item, {allOf: items}, definitions));
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      const properties = shapes.map(candidate => candidate.properties && Object.hasOwn(candidate.properties, key) ? candidate.properties[key] : undefined).filter((candidate): candidate is NormalizationShape => candidate !== undefined);
      const additional = shapes.map(candidate => candidate.additionalProperties).filter((candidate): candidate is NormalizationShape => candidate !== undefined);
      return [key, normalizeValidated(item, {allOf: properties.length ? properties : additional}, definitions)];
    }));
  }
  return value;
}
export function materialize(value: unknown): unknown {
  if (value instanceof LosslessNumber) {
    const token = value.value, number = Number(token);
    if (/[.eE]/.test(token)) {
      if (!Number.isFinite(number)) throw new Error("Non-finite contract number");
      return number;
    }
    return Number.isSafeInteger(number) ? (number === 0 ? 0 : number) : value;
  }
  if (Array.isArray(value)) return value.map(materialize);
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, materialize(item)]));
  return value;
}
/** Reject unsupported values instead of JSON.stringify silently deleting them. */
export function stringifyContractJson(value: unknown): string {
  const active = new Set<object>();
  function encode(item: unknown): string {
    if (item === null || typeof item === "string" || typeof item === "boolean") return JSON.stringify(item);
    if (typeof item === "bigint") return item.toString();
    if (typeof item === "number") {
      if (!Number.isFinite(item)) throw new Error("Non-finite request number");
      if (Object.is(item, -0)) return "-0.0";
      const token = String(item);
      // A caller's unsafe Number has already lost integer identity. Emit a
      // float token so a strict integer contract refuses it; callers can pass
      // an ExactInteger or bigint for the exact permitted integer instead.
      return Number.isInteger(item) && !Number.isSafeInteger(item) && !/[.eE]/.test(token) ? `${token}.0` : token;
    }
    if (item instanceof LosslessNumber) { decimal(item.value); return item.value; }
    if (typeof item !== "object") throw new Error("Unsupported JSON request value");
    if (active.has(item)) throw new Error("Cyclic JSON request"); active.add(item);
    if (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null) throw new Error("Unsupported JSON request object");
    if (Object.getOwnPropertySymbols(item).length) throw new Error("Unsupported JSON request key");
    const result = Array.isArray(item)
      ? `[${Array.from(item, encode).join(",")}]`
      : `{${Object.entries(item).map(([key, child]) => `${JSON.stringify(key)}:${encode(child)}`).join(",")}}`;
    active.delete(item);
    return result;
  }
  return encode(value);
}

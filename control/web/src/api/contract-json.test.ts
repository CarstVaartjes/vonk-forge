import {describe, expect, test, vi} from "vitest";
import {LosslessNumber} from "lossless-json";
import {ContractResponse, ContractResponseTooLarge, readControlResponse, readControlResponseText, validateComponent, validateControlBody} from "./contract-json";
import {UnsupportedContractRuntime, compareNumeric, contractEqual, contractType, numericMultiple, parseContractJson, stringifyContractJson} from "./contract-numeric";

describe("canonical numeric boundary", () => {
  test("native parsing and normalization preserve own prototype keys", () => {
    const text = '{"__proto__":{"private":true},"value":9007199254740993}';
    const parsed = parseContractJson(text);
    expect(stringifyContractJson(parsed)).toBe(text);
    expect(() => validateComponent("BoundedErrorResponse", '{"detail":"retry","__proto__":{"private":true}}')).toThrow();
  });
  test("unsupported source-context engines report a runtime capability cause", () => {
    const nativeParse = JSON.parse;
    const replacement: typeof JSON.parse = (text, reviver) => nativeParse(text, reviver ? (key, value) => reviver(key, value) : undefined);
    const spy = vi.spyOn(JSON, "parse").mockImplementation(replacement);
    try {
      expect(() => parseContractJson("1")).toThrow(UnsupportedContractRuntime);
      expect(() => validateControlBody("GET", "/api/jobs/known", 503, "application/json", '{"detail":1}')).toThrow(UnsupportedContractRuntime);
    } finally { spy.mockRestore(); }
  });
  test.each(["9007199254740993", "18446744073709551615"])("retains permitted integer %s", token => {
    const text = `{"text":"","truncated":false,"dropped_bytes":${token},"dropped_lines":null}`;
    const value = validateComponent("FailureLogTail", text);
    expect(stringifyContractJson(value)).toBe(text);
  });
  test.each(["1.0", "1e0", "-1", "18446744073709551616", "true", '"1"'])("refuses invalid strict counter %s", token => {
    expect(() => validateComponent("FailureLogTail", `{"text":"","truncated":false,"dropped_bytes":${token},"dropped_lines":null}`)).toThrow();
  });
  test("numeric wrappers cannot satisfy object branches", () => {
    const number = parseContractJson("9007199254740993");
    expect(contractType(number, ["object"])).toBe(false);
    expect(contractType({value: "9007199254740993"}, ["integer"])).toBe(false);
    expect(() => validateComponent("BoundedErrorResponse", "1")).toThrow();
    expect(() => validateComponent("BoundedErrorResponse", '{"detail":"refused","undeclared":1}')).toThrow();
  });
  test("compares extreme exponents without allocating expanded decimals", () => {
    expect(compareNumeric("1e999999999", "9e999999998")).toBe(1);
    expect(compareNumeric("1e-999999999", "0")).toBe(1);
    expect(compareNumeric("1e" + "9".repeat(100_000), "1e999999999")).toBe(1);
    expect(contractType(parseContractJson("1e999999999"), ["number"])).toBe(false);
    expect(contractEqual(parseContractJson("1.00e2"), parseContractJson("100"))).toBe(true);
  });
  test("multipleOf uses exact decimal arithmetic", () => {
    expect(numericMultiple(parseContractJson("0.3"), "0.1")).toBe(true);
    expect(numericMultiple(parseContractJson("0.31"), "0.1")).toBe(false);
    expect(numericMultiple(parseContractJson("1e999999999"), "0.25")).toBe(true);
    expect(numericMultiple(parseContractJson("1e-999999999"), "0.25")).toBe(false);
  });
  test("serializer retains exact integers and rejects unsupported values", () => {
    expect(stringifyContractJson({counter: new LosslessNumber("9007199254740993")})).toBe('{"counter":9007199254740993}');
    expect(stringifyContractJson({counter: 9007199254740993n})).toBe('{"counter":9007199254740993}');
    expect(stringifyContractJson({isLosslessNumber: true, value: "9007199254740993"})).toBe('{"isLosslessNumber":true,"value":"9007199254740993"}');
    expect(stringifyContractJson({value: -0})).toBe('{"value":-0.0}');
    for (const value of [NaN, Infinity, {value: undefined}, {value: () => 1}, {value: Symbol("x")}, new Date()]) expect(() => stringifyContractJson(value)).toThrow();
    const cyclic: unknown[] = []; cyclic.push(cyclic); expect(() => stringifyContractJson(cyclic)).toThrow();
  });
  test("real error envelope accepts known fields and refuses undeclared fields", () => {
    expect(validateControlBody("GET", "/api/jobs/known", 503, "application/json", '{"detail":"retry"}')).toEqual({detail: "retry"});
    expect(() => validateControlBody("GET", "/api/jobs/known", 503, "application/json", '{"detail":"retry","private":true}')).toThrow();
  });
  test("an integer negative-zero token retains canonical integer zero", () => {
    const value = validateComponent("RecipeSetting", '{"value":-0,"change_effect":"restart"}');
    expect(stringifyContractJson(value)).toBe('{"value":0,"change_effect":"restart"}');
  });
  test.each(["1000.0", "-0.0", "1.00000000000000001", "-1e-400"])("scalar export retains its canonical float branch: %s", token => {
    const result = validateComponent("RecipeSetting", `{"value":${token},"change_effect":"restart"}`);
    const exported = stringifyContractJson(result);
    expect(exported).toMatch(/"value":(?:1000\.0|-0\.0|1\.0)/);
    expect(() => validateComponent("RecipeSetting", exported)).not.toThrow();
  });
  test("only a declared bodyless response accepts an empty document", () => {
    expect(validateControlBody("POST", "/api/auth/logout", 204, "", "")).toBeUndefined();
    expect(() => validateControlBody("GET", "/api/jobs/known", 204, "", "")).toThrow();
    expect(() => validateControlBody("POST", "/api/auth/logout", 204, "application/json", "{}")).toThrow();
  });
  test("generated transport response consumes once and retains metadata", async () => {
    const source = new Response('{"counter":9007199254740993}', {status: 200, headers: {"x-request-id": "example"}});
    const text = await source.text(), value = parseContractJson(text);
    const response = new ContractResponse(source, value, text), clone = response.clone(), secondClone = clone.clone();
    expect(response.headers.get("x-request-id")).toBe("example");
    expect(stringifyContractJson(await response.json())).toBe(text);
    expect(response.bodyUsed).toBe(true);
    await expect(response.json()).rejects.toThrow();
    expect(stringifyContractJson(await clone.json())).toBe(text);
    expect(stringifyContractJson(await secondClone.json())).toBe(text);
    expect(() => response.clone()).toThrow();
  });
});


describe("producer-owned streamed response budgets", () => {
  const budget = 1048576; // Canonical operation/library producer allocation.
  test("refuses a declared oversized body before reading and cancels the stream", async () => {
    const pull = vi.fn(), cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({pull, cancel}, {highWaterMark: 0});
    const response = new Response(body, {headers: {"content-length": String(budget + 1)}});
    await expect(readControlResponseText(response, "GET", "/api/operations")).rejects.toBeInstanceOf(ContractResponseTooLarge);
    expect(pull).not.toHaveBeenCalled();
    expect(cancel).toHaveBeenCalledOnce();
  });
  test.each([undefined, "1"])("counts actual bytes when content length is %s", async declared => {
    let sent = 0;
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      pull(controller) { sent += 1; controller.enqueue(new Uint8Array(budget / 2 + 1)); }, cancel,
    }, {highWaterMark: 0});
    const response = new Response(body, {headers: declared === undefined ? {} : {"content-length": declared}});
    await expect(readControlResponseText(response, "GET", "/api/operations")).rejects.toBeInstanceOf(ContractResponseTooLarge);
    expect(sent).toBe(2);
    expect(cancel).toHaveBeenCalledOnce();
  });
  test("decodes a multibyte token split between chunks before canonical validation", async () => {
    const text = '{"operations":[{"id":"operation","node_ids":[],"kind":"probe","state":"running","attempt":1,"created_at":"2026-10-07T00:00:00Z","progress":{"phase":"🧱","completed_bytes":0,"total_bytes_known":false}}],"total":1}';
    const bytes = new TextEncoder().encode(text), split = bytes.indexOf(0xf0) + 1;
    const body = new ReadableStream<Uint8Array>({start(controller) {
      controller.enqueue(bytes.slice(0, split)); controller.enqueue(bytes.slice(split)); controller.close();
    }});
    const response = new Response(body, {headers: {"content-type": "application/json"}});
    const value = await readControlResponse(response, "GET", "/api/operations");
    expect(stringifyContractJson(value)).toContain('"phase":"🧱"');
    expect(response.bodyUsed).toBe(true);
  });
  test("does not infer a blanket cap for an unbounded whole-fleet route", async () => {
    const text = "x".repeat(budget + 1);
    expect(await readControlResponseText(new Response(text), "GET", "/api/fleet")).toBe(text);
  });
});

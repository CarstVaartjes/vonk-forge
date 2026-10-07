import {describe, expect, test} from "vitest";
import {LosslessNumber} from "lossless-json";
import {ContractResponse, validateComponent, validateControlBody} from "./contract-json";
import {compareNumeric, contractEqual, contractType, numericMultiple, parseContractJson, stringifyContractJson} from "./contract-numeric";

describe("canonical numeric boundary", () => {
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

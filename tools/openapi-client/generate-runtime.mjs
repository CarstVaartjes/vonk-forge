/** Canonical OpenAPI -> standalone validators and exact numeric DTOs. */
import fs from "node:fs";
import crypto from "node:crypto";
import {fileURLToPath} from "node:url";
import path from "node:path";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import standalone from "ajv/dist/standalone/index.js";
import {_Code, _} from "ajv/dist/compile/codegen/index.js";
import {parse, stringify, LosslessNumber} from "lossless-json";
import openapiTS, {astToString} from "openapi-typescript";
import ts from "typescript";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const input = process.argv[2] ?? path.join(root, "control/openapi.json");
const raw = fs.readFileSync(input, "utf8"), document = parse(raw);
const out = path.join(root, "control/web/src/api");
const token = value => value instanceof LosslessNumber ? value.value : String(value);
const numericKeywords = ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"];
function transform(value) {
  if (Array.isArray(value)) return value.map(transform);
  if (value instanceof LosslessNumber) return Number(value.value);
  if (value === null || typeof value !== "object") return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (["properties", "$defs", "definitions", "patternProperties", "dependentSchemas"].includes(key)) result[key] = Object.fromEntries(Object.entries(item).map(([name, schema]) => [name, transform(schema)]));
    else if (key === "type") result.vonkType = Array.isArray(item) ? item : [item];
    else if (numericKeywords.includes(key)) {
      if (key === "multipleOf") result.vonkMultiple = token(item);
      else (result.vonkBounds ??= []).push([key, token(item)]);
    } else if (key === "const") result.vonkEnum = [stringify(item)];
    else if (key === "enum") result.vonkEnum = item.map(entry => stringify(entry));
    else if (!["discriminator", "example", "examples", "xml", "externalDocs"].includes(key)) result[key] = transform(item);
  }
  return result;
}
const ajv = new Ajv2020({strict: false, allErrors: true, code: {source: true, esm: true, formats: new _Code("fullFormats")}, validateFormats: true});
addFormats(ajv);
for (const [keyword, helper] of [["vonkType", "contractType"], ["vonkEnum", "contractEnum"], ["vonkMultiple", "numericMultiple"]]) {
  ajv.addKeyword({keyword, code(context) {
    const func = context.gen.scopeValue("func", {ref: () => true, code: new _Code(helper)});
    context.fail(_`!${func}(${context.data}, ${new _Code(JSON.stringify(context.schema))})`);
  }});
}
ajv.addKeyword({keyword: "vonkBounds", code(context) {
  const func = context.gen.scopeValue("func", {ref: () => true, code: new _Code("numericBound")});
  for (const [operator, bound] of context.schema) context.fail(_`!${func}(${context.data}, ${bound}, ${operator})`);
}});
const schemas = Object.fromEntries(Object.entries(document.components?.schemas ?? {}).map(([name, schema]) => [name, transform(schema)]));
const exports = {}, routes = [];
const componentId = "urn:vonk:control:components";
ajv.addSchema({components: {schemas}}, componentId);
function bindReferences(value) {
  if (Array.isArray(value)) return value.map(bindReferences);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, key === "$ref" && typeof item === "string" && item.startsWith("#/components/") ? componentId + item : bindReferences(item)]));
}
function register(schema, name) {
  const id = `urn:vonk:control:${name}`;
  ajv.addSchema(bindReferences(transform(schema)), id); exports[name] = id;
}
let index = 0;
for (const [route, item] of Object.entries(document.paths)) {
  for (const [method, operation] of Object.entries(item)) {
    if (!operation || typeof operation !== "object" || !operation.responses) continue;
    const responses = {}, requests = {};
    for (const [status, response] of Object.entries(operation.responses)) for (const [media, content] of Object.entries(response.content ?? {})) {
      if (!content.schema) continue;
      const name = `validate${index++}`; register(content.schema, name);
      (responses[status] ??= {})[media] = name;
    }
    for (const [media, content] of Object.entries(operation.requestBody?.content ?? {})) {
      if (!content.schema) continue;
      const name = `validate${index++}`; register(content.schema, name); requests[media] = name;
    }
    routes.push({route, method: method.toUpperCase(), responses, requests});
  }
}
for (const name of Object.keys(document.components?.schemas ?? {})) register({$ref: `#/components/schemas/${name}`}, `component${name}`);
const imports = 'import {fullFormats} from "ajv-formats/dist/formats";\nimport {contractType, contractEnum, numericBound, numericMultiple} from "./contract-numeric";\n';
const code = standalone(ajv, exports);
const provenance = `// Generated from canonical OpenAPI SHA256 ${crypto.createHash("sha256").update(raw).digest("hex")}. Do not edit.\n`;
// Ajv's generated code is JavaScript. Keep it JavaScript instead of inventing
// annotations or suppressing TypeScript errors in a generated .ts file.
fs.writeFileSync(path.join(out, "runtime.generated.d.ts"), provenance + Object.keys(exports).map(name => `export const ${name}: ((value: unknown) => boolean) & {errors?: readonly {keyword: string; instancePath: string}[] | null};`).join("\n") + '\nexport const contractRoutes: readonly {route: string; method: string; responses: Record<string, Record<string, (value: unknown) => boolean>>; requests: Record<string, (value: unknown) => boolean>}[];\n');
// Route tables refer to the actual exported functions, never string names.
const routeCode = JSON.stringify(routes).replace(/"(validate[0-9]+)"/g, "$1");
const destination = path.join(out, "runtime.generated.js");
fs.writeFileSync(destination, provenance + imports + code + `\nexport const contractRoutes = ${routeCode};\n`);
const node = ts.factory;
const ast = await openapiTS(JSON.parse(raw), {transform(schema) {
  if (schema.type !== "integer" && schema.type !== "number") return;
  const lower = schema.minimum ?? schema.exclusiveMinimum, upper = schema.maximum ?? schema.exclusiveMaximum;
  if (typeof lower === "number" && typeof upper === "number" && Number.isSafeInteger(lower) && Number.isSafeInteger(upper)) return;
  return node.createUnionTypeNode([node.createKeywordTypeNode(ts.SyntaxKind.NumberKeyword), node.createTypeReferenceNode("ExactInteger")]);
}});
fs.writeFileSync(path.join(out, "generated.d.ts"), provenance + 'import type {ExactInteger} from "./contract-numeric";\n' + astToString(ast));

/** Canonical OpenAPI -> standalone validators and exact numeric DTOs. */
import fs from "node:fs";
import crypto from "node:crypto";
import {fileURLToPath} from "node:url";
import path from "node:path";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import standalone from "ajv/dist/standalone/index.js";
import codegen from "ajv/dist/compile/codegen/code.js";
import {parse, stringify, LosslessNumber} from "lossless-json";
import openapiTS, {astToString} from "openapi-typescript";
import ts from "typescript";
const {_Code, _} = codegen;

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
// The official JSON Schema corpus exercises mathematical integer semantics;
// production Control contracts additionally require strict integer lexemes.
const schemaOnly = process.argv[2] === "--schema";
const input = process.argv[schemaOnly ? 3 : 2] ?? path.join(root, "control/openapi.json");
const raw = fs.readFileSync(input, "utf8"), parsed = parse(raw);
const document = schemaOnly ? {components: {schemas: {Suite: parsed}}, paths: {}} : parsed;
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
      else (result.vonkBounds ??= []).push([key, token(item), !schemaOnly && value.type === "number"]);
    } else if (key === "const") result.vonkEnum = [stringify(item)];
    else if (key === "enum") result.vonkEnum = item.map(entry => stringify(entry));
    else if (!["discriminator", "example", "examples", "xml", "externalDocs"].includes(key)) result[key] = transform(item);
  }
  // JSON Schema object keywords ignore numbers. Ajv sees the lossless token
  // class as an object, so apply these keywords only to actual JSON objects.
  const objectKeywords = ["properties", "patternProperties", "additionalProperties", "propertyNames", "required", "minProperties", "maxProperties", "dependentRequired", "dependentSchemas", "dependencies", "unevaluatedProperties"];
  const objectRules = {};
  for (const key of objectKeywords) if (Object.hasOwn(result, key)) { objectRules[key] = result[key]; delete result[key]; }
  if (Object.keys(objectRules).length) (result.allOf ??= []).push({if: {vonkType: ["object"]}, then: objectRules});
  return result;
}
const ajv = new Ajv2020({strict: false, allErrors: false, inlineRefs: false, code: {source: true, esm: true, formats: new _Code("fullFormats")}, validateFormats: true});
addFormats(ajv);
for (const [keyword, helper] of [["vonkType", "contractType"], ["vonkEnum", "contractEnum"], ["vonkMultiple", "numericMultiple"]]) {
  ajv.addKeyword({keyword, code(context) {
    const func = context.gen.scopeValue("func", {ref: () => true, code: new _Code(helper)});
    context.fail(keyword === "vonkType"
      ? _`!${func}(${context.data}, ${new _Code(JSON.stringify(context.schema))}, ${!schemaOnly}, ${!schemaOnly})`
      : _`!${func}(${context.data}, ${new _Code(JSON.stringify(context.schema))})`);
  }});
}
ajv.addKeyword({keyword: "vonkBounds", code(context) {
  const func = context.gen.scopeValue("func", {ref: () => true, code: new _Code("numericBound")});
  for (const [operator, bound, ieeeFloat] of context.schema) context.fail(_`!${func}(${context.data}, ${bound}, ${operator}, ${ieeeFloat})`);
}});
const schemas = Object.fromEntries(Object.entries(document.components?.schemas ?? {}).map(([name, schema]) => [name, transform(schema)]));
const exports = {}, routes = [];
const normalization = {};
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
function shape(schema) {
  if (typeof schema !== "object" || schema === null) return {};
  if (schema.$ref) return {ref: schema.$ref.replace(/^#\/components\/schemas\//, "")};
  const result = {};
  if (schema.type) result.type = schema.type;
  if (schema.anyOf || schema.oneOf) result.alternatives = (schema.anyOf ?? schema.oneOf).map(option => {
    const name = `normalize${index++}`; register(option, name);
    const descriptor = shape(option); normalization[name] = descriptor;
    return {validate: name, shape: descriptor};
  });
  if (schema.allOf) result.allOf = schema.allOf.map(shape);
  if (schema.properties) result.properties = Object.fromEntries(Object.entries(schema.properties).map(([name, property]) => [name, shape(property)]));
  if (schema.items && !Array.isArray(schema.items)) result.items = shape(schema.items);
  if (typeof schema.additionalProperties === "object") result.additionalProperties = shape(schema.additionalProperties);
  return result;
}
for (const [route, item] of Object.entries(document.paths)) {
  for (const [method, operation] of Object.entries(item)) {
    if (!operation || typeof operation !== "object" || !operation.responses) continue;
    const responses = {}, requests = {}, parameters = {};
    for (const location of ["path", "query"]) {
      const declared = [...(item.parameters ?? []), ...(operation.parameters ?? [])].filter(parameter => parameter.in === location && parameter.schema);
      if (!declared.length) continue;
      const name = `validate${index++}`;
      const schema = {type: "object", properties: Object.fromEntries(declared.map(parameter => [parameter.name, parameter.schema])), required: declared.filter(parameter => parameter.required).map(parameter => parameter.name), additionalProperties: false};
      register(schema, name); normalization[name] = shape(schema); parameters[location] = name;
    }
    for (const [status, response] of Object.entries(operation.responses)) {
      responses[status] = {};
      for (const [media, content] of Object.entries(response.content ?? {})) {
      if (!content.schema) continue;
      const name = `validate${index++}`; register(content.schema, name);
      normalization[name] = shape(content.schema);
      responses[status][media] = name;
      }
    }
    for (const [media, content] of Object.entries(operation.requestBody?.content ?? {})) {
      if (!content.schema) continue;
      const name = `validate${index++}`; register(content.schema, name); requests[media] = name;
      normalization[name] = shape(content.schema);
    }
    routes.push({route, method: method.toUpperCase(), responses, requests, parameters});
  }
}
const shapes = {};
if (schemaOnly) register(parsed, "componentSuite");
else for (const [name, schema] of Object.entries(document.components?.schemas ?? {})) {
  register({$ref: `#/components/schemas/${name}`}, `component${name}`);
  shapes[name] = shape(schema); normalization[`component${name}`] = {ref: name};
}
const imports = 'import {fullFormats} from "ajv-formats/dist/formats";\nimport {contractType, contractEnum, numericBound, numericMultiple, normalizeValidated} from "./contract-numeric";\n';
const code = standalone(ajv, exports);
const provenance = `// Generated from canonical OpenAPI SHA256 ${crypto.createHash("sha256").update(raw).digest("hex")}. Do not edit.\n`;
if (schemaOnly) {
  fs.writeFileSync(process.argv[4], provenance + imports + code + "\nexport {componentSuite as validateSchema};\n");
  process.exit(0);
}
// Ajv's generated code is JavaScript. Keep it JavaScript instead of inventing
// annotations or suppressing TypeScript errors in a generated .ts file.
fs.writeFileSync(path.join(out, "runtime.generated.d.ts"), provenance + 'type Validator = ((value: unknown) => boolean) & {normalize: (value: unknown) => unknown; errors?: readonly {keyword: string; instancePath: string}[] | null};\n' + Object.keys(exports).map(name => `export const ${name}: Validator;`).join("\n") + '\nexport const contractRoutes: readonly {route: string; method: string; responses: Record<string, Record<string, Validator>>; requests: Record<string, Validator>; parameters: Record<string, Validator>}[];\n');
// Route tables refer to the actual exported functions, never string names.
const routeCode = JSON.stringify(routes).replace(/"(validate[0-9]+)"/g, "$1");
const destination = path.join(out, "runtime.generated.js");
const descriptor = value => JSON.stringify(value).replace(/"validate":"(normalize[0-9]+)"/g, '"validate":$1');
const normalizers = `\nconst shapes = ${descriptor(shapes)};\n` + Object.entries(normalization).map(([name, node]) => `Object.assign(${name}, {normalize: value => normalizeValidated(value, ${descriptor(node)}, shapes)});`).join("\n");
fs.writeFileSync(destination, provenance + imports + code + normalizers + `\nexport const contractRoutes = ${routeCode};\n`);
const node = ts.factory;
const ast = await openapiTS(JSON.parse(raw), {transform(schema) {
  if (schema.type !== "integer" && schema.type !== "number") return;
  const lower = schema.minimum ?? schema.exclusiveMinimum, upper = schema.maximum ?? schema.exclusiveMaximum;
  if (typeof lower === "number" && typeof upper === "number" && Number.isSafeInteger(lower) && Number.isSafeInteger(upper)) return;
  return node.createUnionTypeNode([node.createKeywordTypeNode(ts.SyntaxKind.NumberKeyword), node.createTypeReferenceNode("ExactInteger")]);
}});
fs.writeFileSync(path.join(out, "generated.d.ts"), provenance + 'import type {ExactInteger} from "./contract-numeric";\n' + astToString(ast));

// Wire-message validator for the Mortic sidepod <-> engine protocol v0.
// Interprets the generated JSON Schema in protocol.gen.mjs (source of truth:
// protocol/schema.ts). Dependency-free on purpose: the plugin ships src/ only.
//
// Contract semantics (docs/MORTIC_PROTOCOL_V0.md):
// - unknown fields on known message types pass (loose objects),
// - unknown message types are reported as `unknownType` so callers can log
//   and ignore them instead of treating them as invalid.
import { PROTOCOL_SCHEMA } from "./protocol.gen.mjs";

function typeOf(value) {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  return typeof value;
}

function matchesType(expected, value) {
  const actual = typeOf(value);
  if (expected === "integer") return actual === "number" && Number.isInteger(value);
  return actual === expected;
}

function validateNode(schema, value, path, errors) {
  if (!schema || typeof schema !== "object" || Object.keys(schema).length === 0) {
    return; // empty schema accepts anything (loose additionalProperties)
  }
  if (schema.type && !matchesType(schema.type, value)) {
    errors.push(`${path}: expected ${schema.type}, got ${typeOf(value)}`);
    return;
  }
  if ("const" in schema && value !== schema.const) {
    errors.push(`${path}: expected ${JSON.stringify(schema.const)}`);
    return;
  }
  if (schema.enum && !schema.enum.includes(value)) {
    errors.push(`${path}: expected one of ${schema.enum.join(", ")}`);
    return;
  }
  if (schema.minLength !== undefined && typeof value === "string" && value.length < schema.minLength) {
    errors.push(`${path}: shorter than ${schema.minLength}`);
    return;
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) {
      errors.push(`${path}: below minimum ${schema.minimum}`);
      return;
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      errors.push(`${path}: above maximum ${schema.maximum}`);
      return;
    }
  }
  if (schema.type === "object") {
    for (const field of schema.required ?? []) {
      if (!(field in value)) errors.push(`${path}.${field}: missing required field`);
    }
    for (const [field, fieldSchema] of Object.entries(schema.properties ?? {})) {
      if (field in value) validateNode(fieldSchema, value[field], `${path}.${field}`, errors);
    }
  }
}

/**
 * Validate one wire message. `direction` is "command" (sidepod -> engine) or
 * "event" (engine -> sidepod).
 * Returns { ok:true } | { ok:false, unknownType:true } | { ok:false, errors:[...] }.
 */
export function checkMessage(direction, payload) {
  if (typeOf(payload) !== "object" || typeof payload.type !== "string") {
    return { ok: false, errors: ["$: not an object with a string `type`"] };
  }
  const table = direction === "command" ? PROTOCOL_SCHEMA.commands : PROTOCOL_SCHEMA.events;
  const schema = table[payload.type];
  if (!schema) return { ok: false, unknownType: true };
  const errors = [];
  validateNode(schema, payload, "$", errors);
  return errors.length === 0 ? { ok: true } : { ok: false, errors };
}

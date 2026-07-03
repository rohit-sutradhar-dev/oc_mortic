import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  PROTOCOL_SCHEMA,
  PROTOCOL_VERSION,
  SOURCE_HASH,
  COMMAND_TYPES,
  EVENT_TYPES,
} from "../src/protocol.gen.mjs";
import { checkMessage } from "../src/protocol-validate.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const fixture = JSON.parse(
  readFileSync(join(repoRoot, "tests", "fixtures", "protocol_v0_messages.json"), "utf8"),
);

const byName = (entries) => Object.fromEntries(entries.map((e) => [e.name, e.message]));

test("every fixture message validates against the shared schema", () => {
  assert.equal(fixture.protocolVersion, PROTOCOL_VERSION);
  assert.deepEqual(Object.keys(byName(fixture.commands)).sort(), [...COMMAND_TYPES].sort());
  assert.deepEqual(Object.keys(byName(fixture.events)).sort(), [...EVENT_TYPES].sort());

  for (const [name, message] of Object.entries(byName(fixture.commands))) {
    const check = checkMessage("command", message);
    assert.deepEqual(check, { ok: true }, `${name}: ${JSON.stringify(check.errors)}`);
  }
  for (const [name, message] of Object.entries(byName(fixture.events))) {
    const check = checkMessage("event", message);
    assert.deepEqual(check, { ok: true }, `${name}: ${JSON.stringify(check.errors)}`);
  }
  for (const example of fixture.screenOnlyExamples) {
    assert.equal(checkMessage("event", example.message).ok, true, example.name);
  }
});

test("validator rejects mutations and flags unknown types", () => {
  const commands = byName(fixture.commands);
  const events = byName(fixture.events);

  const missingRequired = { ...commands["live.set"] };
  delete missingRequired.value;
  assert.equal(checkMessage("command", missingRequired).ok, false);

  const wrongType = { ...events.transcript, sequence: "one" };
  assert.equal(checkMessage("event", wrongType).ok, false);

  const wrongConst = { ...commands.start, protocolVersion: "mortic.sidepod.v1" };
  assert.equal(checkMessage("command", wrongConst).ok, false);

  const unknown = checkMessage("event", { type: "speech.telemetry", sentAt: "2026-07-02T04:00:00.000Z" });
  assert.deepEqual(unknown, { ok: false, unknownType: true });

  const extraField = { ...events.ready, futureField: { nested: true } };
  assert.equal(checkMessage("event", extraField).ok, true, "unknown fields on known types must pass");

  const directionMatters = checkMessage("command", events.ready);
  assert.equal(directionMatters.ok, false, "events are not valid commands");
});

test("generated artifacts match the TypeScript schema source", () => {
  const sourceHash = createHash("sha256")
    .update(readFileSync(join(repoRoot, "protocol", "schema.ts")))
    .digest("hex");
  assert.equal(
    SOURCE_HASH,
    sourceHash,
    "protocol/schema.ts changed: run `npm run gen` in protocol/",
  );

  const canonical = JSON.parse(
    readFileSync(join(repoRoot, "protocol", "mortic.sidepod.v0.schema.json"), "utf8"),
  );
  assert.equal(canonical["x-source-hash"], SOURCE_HASH);
  assert.deepEqual(canonical, PROTOCOL_SCHEMA);
});

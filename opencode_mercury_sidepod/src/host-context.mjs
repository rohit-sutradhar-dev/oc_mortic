// Bridges the plugin's hook entry (src/index.js, which receives PluginInput
// including serverUrl) to the TUI entry (src/tui.js, which does not). The two
// entries can be loaded as separate module graphs (verified live: module
// state did NOT cross), so the value is carried on process env — process
// state is global to the plugin host and inherited by the spawned helper.
// A user-set OPENCODE_VOICE_OPENCODE_URL always wins as an explicit override.
import { appendFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const RECORDED_ENV = "MORTIC_OPENCODE_SERVER_URL";

// Observability sink shared by both entries: append one JSON line per record
// (event names and diagnostics only — never transcript or reply content).
// MORTIC_SMOKE_LOG overrides the path; otherwise a default sink is always on,
// because a live-session viewer bug is undiagnosable without the client's
// receive/validate/apply record (learned 2026-07-04: a spoken turn rendered
// nothing while every engine-side layer machine-verified clean).
const DEFAULT_SINK = join(tmpdir(), "mortic-plugin-smoke.jsonl");
const MAX_SINK_BYTES = 2_000_000;
let writesSinceSizeCheck = 0;

export function appendSmoke(record) {
  const sink = globalThis.process?.env?.MORTIC_SMOKE_LOG || DEFAULT_SINK;
  try {
    if (writesSinceSizeCheck === 0) {
      try {
        if (statSync(sink).size > MAX_SINK_BYTES) {
          writeFileSync(sink, "");
        }
      } catch {
        // missing file is fine; appendFileSync creates it
      }
    }
    writesSinceSizeCheck = (writesSinceSizeCheck + 1) % 200;
    appendFileSync(sink, JSON.stringify({ at: new Date().toISOString(), ...record }) + "\n");
  } catch {
    // the smoke sink must never break plugin load or key handling
  }
}

export function recordServerUrl(url) {
  if (url && globalThis.process?.env) {
    globalThis.process.env[RECORDED_ENV] = String(url).replace(/\/$/, "");
  }
}

export function opencodeServerUrl(env = globalThis.process?.env ?? {}) {
  return env.OPENCODE_VOICE_OPENCODE_URL ?? env[RECORDED_ENV];
}

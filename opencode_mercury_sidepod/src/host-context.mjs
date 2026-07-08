// Shared diagnostics for the plugin hook and TUI entry. The hook's serverUrl
// is useful evidence, but a bare OpenCode TUI can advertise a URL that is not
// TCP-reachable, so v1 does not route through it. A user-set
// OPENCODE_VOICE_OPENCODE_URL remains an explicit dev/debug override.
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

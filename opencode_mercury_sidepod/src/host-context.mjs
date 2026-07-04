// Bridges the plugin's hook entry (src/index.js, which receives PluginInput
// including serverUrl) to the TUI entry (src/tui.js, which does not). The two
// entries can be loaded as separate module graphs (verified live: module
// state did NOT cross), so the value is carried on process env — process
// state is global to the plugin host and inherited by the spawned helper.
// A user-set OPENCODE_VOICE_OPENCODE_URL always wins as an explicit override.
import { appendFileSync } from "node:fs";

const RECORDED_ENV = "MORTIC_OPENCODE_SERVER_URL";

// Test-observability sink shared by both entries: append one JSON line to
// MORTIC_SMOKE_LOG, and never let the sink break the plugin.
export function appendSmoke(record) {
  const sink = globalThis.process?.env?.MORTIC_SMOKE_LOG;
  if (!sink) {
    return;
  }
  try {
    appendFileSync(sink, JSON.stringify(record) + "\n");
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

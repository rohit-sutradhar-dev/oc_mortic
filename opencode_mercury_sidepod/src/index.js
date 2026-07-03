import { appendFileSync } from "node:fs";

import { recordServerUrl } from "./host-context.mjs";

const plugin = {
  id: "mortic-sidepod",
  // The hook entry receives PluginInput (including serverUrl) when the host
  // invokes it; the TUI entry reads the recorded value via host-context to
  // pin the helper to the OpenCode server that owns the focused thread.
  server: async (input) => {
    recordServerUrl(input?.serverUrl);
    const sink = globalThis.process?.env?.MORTIC_SMOKE_LOG;
    if (sink) {
      try {
        appendFileSync(
          sink,
          JSON.stringify({ event: "hook.server-url", present: Boolean(input?.serverUrl) }) + "\n"
        );
      } catch {
        // the smoke sink must never break plugin load
      }
    }
    return {};
  }
};

export default plugin;

import { appendSmoke, recordServerUrl } from "./host-context.mjs";

const plugin = {
  id: "mortic-sidepod",
  // The hook entry receives PluginInput (including serverUrl) when the host
  // invokes it; the TUI entry reads the recorded value via host-context to
  // pin the helper to the OpenCode server that owns the focused thread.
  server: async (input) => {
    recordServerUrl(input?.serverUrl);
    appendSmoke({ event: "hook.server-url", present: Boolean(input?.serverUrl) });
    return {};
  }
};

export default plugin;

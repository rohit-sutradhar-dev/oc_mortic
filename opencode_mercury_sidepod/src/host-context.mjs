// Shared state between the plugin's hook entry (src/index.js, which receives
// PluginInput including serverUrl) and the TUI entry (src/tui.js, which does
// not). Both entries load in the same process, so a module-level slot is the
// simplest bridge. Stays undefined when the host never calls the hook entry
// with input; callers fall back to env or helper self-discovery.
const context = { serverUrl: undefined };

export function recordServerUrl(url) {
  if (url) {
    context.serverUrl = String(url).replace(/\/$/, "");
  }
}

export function opencodeServerUrl() {
  return context.serverUrl;
}

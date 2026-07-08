import { appendSmoke, recordServerUrl } from "./host-context.mjs";

const plugin = {
  id: "mortic-sidepod",
  // The hook entry receives PluginInput (including serverUrl) when the host
  // invokes it. Live evidence shows this URL can be nominal rather than
  // TCP-reachable, so it is recorded for diagnostics/dev attach only; v1's
  // happy path starts a Mortic-owned managed voice server.
  server: async (input) => {
    recordServerUrl(input?.serverUrl);
    appendSmoke({ event: "hook.server-url", present: Boolean(input?.serverUrl) });
    return {};
  }
};

export default plugin;

import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import {
  buildHelperEnv,
  helperCwd,
  helperUrl,
  helperWsUrl,
  isReadyPayload,
  resolveHelperCommand,
  tokenizeCommand
} from "../src/helper-launcher.mjs";
import { opencodeServerUrl } from "../src/host-context.mjs";

test("readiness is ready:true and nothing else gates it", () => {
  // MOR-167 contract: externally started helpers are first-class, so the
  // health check must not require side-channel fields like a matching
  // event-log path (that gating refused legitimate helpers on feature-voice).
  assert.equal(isReadyPayload({ ready: true }), true);
  assert.equal(isReadyPayload({ ready: true, event_log_path: "/anything/else.jsonl" }), true);
  assert.equal(isReadyPayload({ ready: false }), false);
  assert.equal(isReadyPayload({ ok: true }), false);
  assert.equal(isReadyPayload(undefined), false);
});

test("command override survives paths with spaces", () => {
  // Found live: this repo's path contains spaces, and a naive whitespace
  // split turned the override into a nonexistent command (silent ENOENT).
  assert.deepEqual(
    tokenizeCommand('"/Users/dev/My Repo/.venv/bin/mortic-helper" --flag'),
    ["/Users/dev/My Repo/.venv/bin/mortic-helper", "--flag"]
  );
  assert.deepEqual(tokenizeCommand("uv run mortic-helper"), ["uv", "run", "mortic-helper"]);
  assert.deepEqual(tokenizeCommand("'/tmp/spaced dir/bin' -x"), ["/tmp/spaced dir/bin", "-x"]);

  const override = resolveHelperCommand({
    env: { MORTIC_HELPER_CMD: '"/Users/dev/My Repo/.venv/bin/mortic-helper"' },
    repoRoot: "/repo",
    exists: () => false
  });
  assert.equal(override.command, "/Users/dev/My Repo/.venv/bin/mortic-helper");
});

test("launch resolution honors override, dev install, repo checkout, then uvx", () => {
  const repoRoot = "/repo";
  const none = () => false;

  const override = resolveHelperCommand({
    env: { MORTIC_HELPER_CMD: "python -m opencode_voice" },
    repoRoot,
    exists: none
  });
  assert.deepEqual(override, { command: "python", args: ["-m", "opencode_voice"], source: "env" });

  const venv = resolveHelperCommand({
    env: {},
    repoRoot,
    exists: (path) => path === join(repoRoot, ".venv", "bin", "mortic-helper")
  });
  assert.equal(venv.source, "venv");
  assert.equal(venv.command, join(repoRoot, ".venv", "bin", "mortic-helper"));

  const project = resolveHelperCommand({
    env: {},
    repoRoot,
    exists: (path) => path === join(repoRoot, "pyproject.toml") || path === join(repoRoot, "opencode_voice")
  });
  assert.deepEqual(project, {
    command: "uv",
    args: ["run", "--project", repoRoot, "mortic-helper"],
    source: "uv-project"
  });

  const published = resolveHelperCommand({ env: {}, repoRoot, exists: none });
  assert.deepEqual(published, { command: "uvx", args: ["mortic-helper"], source: "uvx" });
});

test("the spawned helper is pinned to the focused thread's OpenCode server", () => {
  const env = buildHelperEnv({ env: { PATH: "/usr/bin" }, opencodeUrl: "http://127.0.0.1:4242" });
  assert.equal(env.OPENCODE_VOICE_OPENCODE_URL, "http://127.0.0.1:4242");
  assert.equal(env.PATH, "/usr/bin");

  const bare = buildHelperEnv({ env: { PATH: "/usr/bin" } });
  assert.equal("OPENCODE_VOICE_OPENCODE_URL" in bare, false);
});

test("repo-checkout launches run from the repo root so BYOK .env loads", () => {
  assert.equal(helperCwd("venv", "/repo"), "/repo");
  assert.equal(helperCwd("uv-project", "/repo"), "/repo");
  assert.equal(helperCwd("uvx", "/repo"), undefined);
  assert.equal(helperCwd("env", "/repo"), undefined);
});

test("the recorded server url bridges entries via process env", () => {
  // The hook and TUI entries load as separate module graphs (verified live),
  // so module-level state cannot carry serverUrl between them; process env
  // can, and it also flows to the spawned helper automatically. An explicit
  // user override always wins.
  assert.equal(
    opencodeServerUrl({ MORTIC_OPENCODE_SERVER_URL: "http://127.0.0.1:5000" }),
    "http://127.0.0.1:5000"
  );
  assert.equal(
    opencodeServerUrl({
      OPENCODE_VOICE_OPENCODE_URL: "http://user-override:1",
      MORTIC_OPENCODE_SERVER_URL: "http://127.0.0.1:5000"
    }),
    "http://user-override:1"
  );
  assert.equal(opencodeServerUrl({}), undefined);
});

test("helper URLs derive from one base with env overrides", () => {
  assert.equal(helperUrl({}), "http://127.0.0.1:8765");
  assert.equal(helperWsUrl({}), "ws://127.0.0.1:8765/ws/sidepod");
  assert.equal(helperUrl({ MORTIC_HELPER_URL: "http://127.0.0.1:9900/" }), "http://127.0.0.1:9900");
  assert.equal(helperWsUrl({ MORTIC_HELPER_URL: "http://127.0.0.1:9900" }), "ws://127.0.0.1:9900/ws/sidepod");
  assert.equal(helperWsUrl({ MORTIC_HELPER_WS_URL: "ws://elsewhere:1/ws/sidepod" }), "ws://elsewhere:1/ws/sidepod");
});

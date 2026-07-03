import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import {
  buildHelperEnv,
  helperUrl,
  helperWsUrl,
  isReadyPayload,
  resolveHelperCommand
} from "../src/helper-launcher.mjs";

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

test("helper URLs derive from one base with env overrides", () => {
  assert.equal(helperUrl({}), "http://127.0.0.1:8765");
  assert.equal(helperWsUrl({}), "ws://127.0.0.1:8765/ws/sidepod");
  assert.equal(helperUrl({ MORTIC_HELPER_URL: "http://127.0.0.1:9900/" }), "http://127.0.0.1:9900");
  assert.equal(helperWsUrl({ MORTIC_HELPER_URL: "http://127.0.0.1:9900" }), "ws://127.0.0.1:9900/ws/sidepod");
  assert.equal(helperWsUrl({ MORTIC_HELPER_WS_URL: "ws://elsewhere:1/ws/sidepod" }), "ws://elsewhere:1/ws/sidepod");
});

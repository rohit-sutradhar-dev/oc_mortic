import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import {
  buildHelperArgs,
  buildHelperEnv,
  healthReason,
  healthMatchesWorkspace,
  helperCwd,
  helperUrl,
  helperWsUrl,
  isReadyPayload,
  probeExistingHelper,
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

test("dev attach can explicitly pin the helper to an existing OpenCode server", () => {
  const env = buildHelperEnv({ env: { PATH: "/usr/bin" }, opencodeUrl: "http://127.0.0.1:4242" });
  assert.equal(env.OPENCODE_VOICE_OPENCODE_URL, "http://127.0.0.1:4242");
  assert.equal(env.PATH, "/usr/bin");

  const bare = buildHelperEnv({ env: { PATH: "/usr/bin" } });
  assert.equal("OPENCODE_VOICE_OPENCODE_URL" in bare, false);
});

test("managed helper startup uses an explicit managed OpenCode server", () => {
  assert.deepEqual(
    buildHelperArgs({ baseArgs: ["run"], port: "8765", managed: true, workspaceDir: "/repo/worktree" }),
    ["run", "--host", "127.0.0.1", "--port", "8765", "--managed-opencode", "--opencode-dir", "/repo/worktree"]
  );
  assert.deepEqual(
    buildHelperArgs({ baseArgs: ["run"], port: "8765", managed: false }),
    ["run", "--host", "127.0.0.1", "--port", "8765", "--no-managed"]
  );
});

test("managed helper reuse requires the same workspace", () => {
  assert.equal(healthMatchesWorkspace({ workspace_dir: "/repo/worktree/" }, "/repo/worktree"), true);
  assert.equal(healthMatchesWorkspace({ workspace_dir: "/other" }, "/repo/worktree"), false);
  assert.equal(healthMatchesWorkspace({ ready: true }, "/repo/worktree"), false);
  assert.equal(healthMatchesWorkspace({ ready: true }, undefined), true);
});

test("existing helper ownership is probed before a managed confirmation", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({ ready: true, workspace_dir: "/other/workspace" })
    });
    const blocked = await probeExistingHelper({ managed: true, workspaceDir: "/repo/worktree" });
    assert.equal(blocked.ready, false);
    assert.equal(blocked.blocked, true);
    assert.equal(blocked.workspaceMismatch, true);
    assert.match(blocked.reason, /another workspace/);

    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({
        ready: false,
        workspace_dir: "/other/workspace",
        issues: [{ safeDetail: "Mortic could not reach its OpenCode voice server." }]
      })
    });
    const unhealthyBlocked = await probeExistingHelper({ managed: true, workspaceDir: "/repo/worktree" });
    assert.equal(unhealthyBlocked.ready, false);
    assert.equal(unhealthyBlocked.blocked, true);
    assert.equal(unhealthyBlocked.workspaceMismatch, true);
    assert.match(unhealthyBlocked.reason, /another workspace/);

    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({ ready: true, workspace_dir: "/repo/worktree/" })
    });
    const ready = await probeExistingHelper({ managed: true, workspaceDir: "/repo/worktree" });
    assert.equal(ready.ready, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("repo-checkout launches run from the repo root so BYOK .env loads", () => {
  assert.equal(helperCwd("venv", "/repo"), "/repo");
  assert.equal(helperCwd("uv-project", "/repo"), "/repo");
  assert.equal(helperCwd("uvx", "/repo"), undefined);
  assert.equal(helperCwd("env", "/repo"), undefined);
});

test("the recorded server url remains available for diagnostics and explicit override wins", () => {
  // The hook-provided serverUrl is diagnostic only in v1 because it may not be
  // TCP-reachable. An explicit user override always wins for dev attach.
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

test("healthReason surfaces the helper's specific cause so offline is never opaque", () => {
  assert.equal(
    healthReason({
      ready: false,
      issues: [
        {
          diagnosticCode: "opencode_unreachable",
          safeDetail: "Mortic could not reach its OpenCode voice server."
        }
      ]
    }),
    "Mortic could not reach its OpenCode voice server."
  );
  assert.equal(healthReason({ ready: true, issues: [] }), undefined);
  assert.equal(healthReason(undefined), undefined);
});

test("helper URLs derive from one base with env overrides", () => {
  assert.equal(helperUrl({}), "http://127.0.0.1:8765");
  assert.equal(helperWsUrl({}), "ws://127.0.0.1:8765/ws/sidepod");
  assert.equal(helperUrl({ MORTIC_HELPER_URL: "http://127.0.0.1:9900/" }), "http://127.0.0.1:9900");
  assert.equal(helperWsUrl({ MORTIC_HELPER_URL: "http://127.0.0.1:9900" }), "ws://127.0.0.1:9900/ws/sidepod");
  assert.equal(helperWsUrl({ MORTIC_HELPER_WS_URL: "ws://elsewhere:1/ws/sidepod" }), "ws://elsewhere:1/ws/sidepod");
});

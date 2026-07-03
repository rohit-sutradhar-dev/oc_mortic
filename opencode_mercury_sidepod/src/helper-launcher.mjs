// Discovers or launches the local Mortic helper for the voice lane.
//
// Contract (MOR-167): the helper serves GET /api/health and is ready when the
// payload says `ready: true` — nothing else gates readiness, so externally
// started helpers (e.g. `uv run mortic-helper` in a terminal) are first-class.
// Launch resolution order:
//   1. MORTIC_HELPER_CMD           explicit command override
//   2. <repo>/.venv/bin/mortic-helper   repo-local dev install
//   3. uv run --project <repo> mortic-helper   repo checkout without .venv
//   4. uvx mortic-helper           published package
import { spawn } from "node:child_process";
import { existsSync, openSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
// src/ -> package root -> repo root (only meaningful in a repo checkout).
const REPO_ROOT = join(HERE, "..", "..");
const DEFAULT_HELPER_URL = "http://127.0.0.1:8765";
const HEALTH_TIMEOUT_MS = 1500;
const STARTUP_TIMEOUT_MS = 10000;
const POLL_INTERVAL_MS = 250;

let spawnedProcess;

export function helperUrl(env = globalThis.process?.env ?? {}) {
  return (env.MORTIC_HELPER_URL ?? DEFAULT_HELPER_URL).replace(/\/$/, "");
}

export function helperWsUrl(env = globalThis.process?.env ?? {}) {
  if (env.MORTIC_HELPER_WS_URL) {
    return env.MORTIC_HELPER_WS_URL;
  }
  return `${helperUrl(env).replace(/^http/, "ws")}/ws/sidepod`;
}

/** Ready means ready — no side-channel requirements on the health payload. */
export function isReadyPayload(payload) {
  return Boolean(payload && payload.ready === true);
}

export function resolveHelperCommand({
  env = globalThis.process?.env ?? {},
  repoRoot = REPO_ROOT,
  exists = existsSync,
} = {}) {
  if (env.MORTIC_HELPER_CMD) {
    const [command, ...args] = env.MORTIC_HELPER_CMD.split(/\s+/).filter(Boolean);
    return { command, args, source: "env" };
  }
  const venvBinary = join(repoRoot, ".venv", "bin", "mortic-helper");
  if (exists(venvBinary)) {
    return { command: venvBinary, args: [], source: "venv" };
  }
  if (exists(join(repoRoot, "pyproject.toml")) && exists(join(repoRoot, "opencode_voice"))) {
    return { command: "uv", args: ["run", "--project", repoRoot, "mortic-helper"], source: "uv-project" };
  }
  return { command: "uvx", args: ["mortic-helper"], source: "uvx" };
}

/** Env for the spawned helper: pin it to the OpenCode server that owns the thread. */
export function buildHelperEnv({ env = globalThis.process?.env ?? {}, opencodeUrl } = {}) {
  const child = { ...env };
  if (opencodeUrl) {
    child.OPENCODE_VOICE_OPENCODE_URL = String(opencodeUrl);
  }
  return child;
}

async function healthy(url, fetchImpl = globalThis.fetch) {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
    const response = await fetchImpl(`${url}/api/health`, { signal: controller.signal });
    clearTimeout(timer);
    if (!response.ok) {
      return false;
    }
    return isReadyPayload(await response.json());
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Ensure a ready helper: reuse a healthy one, otherwise spawn and poll until
 * ready or timeout. Never throws — returns { ready, reused, source }.
 */
export async function ensureHelper({ opencodeUrl, log = () => {} } = {}) {
  const url = helperUrl();
  if (await healthy(url)) {
    log("helper.reuse", { url });
    return { ready: true, reused: true, source: "existing" };
  }

  const resolved = resolveHelperCommand({});
  const port = new URL(url).port || "8765";
  const logPath = globalThis.process?.env?.MORTIC_HELPER_LOG ?? "/tmp/mortic-helper-plugin.log";
  try {
    if (!spawnedProcess || spawnedProcess.exitCode !== null) {
      const sink = openSync(logPath, "a");
      spawnedProcess = spawn(
        resolved.command,
        [...resolved.args, "--host", "127.0.0.1", "--port", port],
        {
          env: buildHelperEnv({ opencodeUrl }),
          stdio: ["ignore", sink, sink],
          detached: false,
        },
      );
      spawnedProcess.on("error", () => {
        spawnedProcess = undefined;
      });
      log("helper.spawn", { source: resolved.source, port });
    }
  } catch {
    log("helper.spawn.failed", { source: resolved.source });
    return { ready: false, reused: false, source: resolved.source };
  }

  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await healthy(url)) {
      log("helper.ready", { source: resolved.source });
      return { ready: true, reused: false, source: resolved.source };
    }
    await sleep(POLL_INTERVAL_MS);
  }
  log("helper.timeout", { source: resolved.source });
  return { ready: false, reused: false, source: resolved.source };
}

export function stopHelper() {
  if (spawnedProcess && spawnedProcess.exitCode === null) {
    try {
      spawnedProcess.kill("SIGTERM");
    } catch {
      // best-effort shutdown of a process we own
    }
  }
  spawnedProcess = undefined;
}

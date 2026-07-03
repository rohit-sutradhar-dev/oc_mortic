import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const rootDir = fileURLToPath(new URL("..", import.meta.url));

async function readPackageJson() {
  return JSON.parse(await readFile(join(rootDir, "package.json"), "utf8"));
}

async function readText(path) {
  return readFile(join(rootDir, path), "utf8");
}

test("package remains installable as an OpenCode TUI plugin", async () => {
  const pkg = await readPackageJson();

  assert.equal(pkg.type, "module");
  assert.equal(pkg.main, "dist/index.js");
  assert.deepEqual(pkg["oc-plugin"], ["tui"]);
  assert.deepEqual(pkg.exports["."], { import: "./dist/index.js" });
  assert.deepEqual(pkg.exports["./tui"], { import: "./dist/tui.js" });
  assert.ok(pkg.files.includes("dist"));
});

test("source and generated dist avoid deprecated command API", async () => {
  const src = await readText("src/tui.js");
  const dist = await readText("dist/tui.js");

  for (const body of [src, dist]) {
    assert.equal(body.includes("api.command"), false);
    assert.match(body, /api\.keymap\.registerLayer/);
    assert.match(body, /api\.mode\.push\("mortic\.sidepod"\)/);
    assert.match(body, /sidebar_content/);
  }
});

test("source preserves current sidepod surface hooks", async () => {
  const src = await readText("src/tui.js");

  for (const expectedText of ["MORTIC", "COMMAND DECK", "COMMS", "Transcript", "Handoff", "Push to Talk"]) {
    assert.match(src, new RegExp(expectedText));
  }
});

test("source exposes MOR-165 slash and terminal smoke hooks", async () => {
  const src = await readText("src/tui.js");

  assert.match(src, /renderer\.useKittyKeyboard/);
  assert.match(src, /\[mortic smoke\]/);
  assert.match(src, /name:\s*"mortic\.ptt\.press"/);
});

test("focus mode locks typing and PTT is a debounced tap toggle", async () => {
  const src = await readText("src/tui.js");
  const dist = await readText("dist/tui.js");

  for (const body of [src, dist]) {
    // Keymap bindings never dispatch on key release, so a keymap eventType
    // filter is dead code. PTT is tap-to-talk/tap-to-stop by product
    // decision (2026-07-03): no keyrelease listener, no Kitty flag changes.
    assert.equal(/eventType:\s*"release",\s*cmd:/.test(body), false);
    assert.equal(body.includes("keyrelease"), false);
    assert.equal(body.includes("enableKittyKeyboard"), false);
    // Typing lock swallows unbound keys before the prompt renderable sees them.
    assert.match(body, /keyInput/);
    assert.match(body, /preventDefault/);
    assert.match(body, /stopPropagation/);
    // Prompt renderable focus is parked while Mortic focus mode is active.
    assert.match(body, /currentFocusedRenderable/);
    // Key repeat arrives as plain presses (terminals without event types
    // cannot mark repeats; OpenTUI normalizes Kitty repeats to presses), so
    // a deliberate stop-tap is distinguished from repeat by a timing window.
    assert.match(body, /PTT_REPEAT_WINDOW_MS/);
  }
});

test("slash registration matches OpenCode 1.17.x reachability rules", async () => {
  const src = await readText("src/tui.js");
  const dist = await readText("dist/tui.js");

  for (const body of [src, dist]) {
    // Slash menu requires a flat slashName on the layer command; the nested
    // legacy shape `slash: { name }` is only honored by deprecated api.command.
    assert.match(body, /slashName:\s*"mortic"/);
    assert.equal(/slash:\s*{/.test(body), false);

    // The palette layer must not be mode-pinned or the prompt's slash menu
    // treats its commands as unreachable. Only the focus-mode layer pins a mode.
    const modePins = body.match(/registerLayer\(\{\s*\n\s*mode:/g) ?? [];
    assert.equal(modePins.length, 1);
    assert.match(body, /mode:\s*"mortic\.sidepod"/);
  }
});

test("normal UI source does not expose provider or runtime names", async () => {
  const src = await readText("src/tui.js");

  for (const forbidden of ["Mercury", "mercury", "Inception", "Deepgram", "runtime"]) {
    assert.equal(src.includes(forbidden), false);
  }
});

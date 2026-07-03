import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const rootDir = fileURLToPath(new URL("..", import.meta.url));
const pkg = JSON.parse(await readFile(join(rootDir, "package.json"), "utf8"));
const src = await readFile(join(rootDir, "src/tui.js"), "utf8");

test("package remains installable as an OpenCode TUI plugin", () => {
  assert.equal(pkg.type, "module");
  assert.equal(pkg.main, "src/index.js");
  assert.deepEqual(pkg["oc-plugin"], ["tui"]);
  assert.deepEqual(pkg.exports["."], { import: "./src/index.js" });
  assert.deepEqual(pkg.exports["./tui"], { import: "./src/tui.js" });
  assert.ok(pkg.files.includes("src"));
});

test("source avoids deprecated command API", () => {
  assert.equal(src.includes("api.command"), false);
  assert.match(src, /api\.keymap\.registerLayer/);
  assert.match(src, /api\.mode\.push\("mortic\.sidepod"\)/);
  assert.match(src, /sidebar_content/);
});

test("source preserves current sidepod surface hooks", () => {
  for (const expectedText of ["MORTIC", "COMMAND DECK", "COMMS", "Transcript", "Handoff", "Push to Talk"]) {
    assert.match(src, new RegExp(expectedText));
  }
});

test("source exposes MOR-165 slash and terminal smoke hooks", () => {
  assert.match(src, /renderer\.useKittyKeyboard/);
  assert.match(src, /\[mortic smoke\]/);
  assert.match(src, /name:\s*"mortic\.ptt\.press"/);
});

test("focus mode locks typing and PTT is a plain M toggle", () => {
  // PTT is a plain M toggle by product decision (2026-07-03): every press
  // flips armed/stopped. No keyrelease listener, no Kitty flag changes,
  // no repeat debounce, no event-type handling.
  assert.equal(/eventType:\s*"release",\s*cmd:/.test(src), false);
  assert.equal(src.includes("keyrelease"), false);
  assert.equal(src.includes("enableKittyKeyboard"), false);
  assert.equal(src.includes("PTT_REPEAT_WINDOW_MS"), false);
  // Typing lock swallows unbound keys before the prompt renderable sees them.
  assert.match(src, /keyInput/);
  assert.match(src, /preventDefault/);
  assert.match(src, /stopPropagation/);
  // Prompt renderable focus is parked while Mortic focus mode is active.
  assert.match(src, /currentFocusedRenderable/);
});

test("popups are centered host dialogs and Esc is never destructive", () => {
  // Owner spec 2026-07-03: Transcript, Handoff, and End Session render as
  // centered host dialogs, never inside the sidepod under COMMS.
  assert.match(src, /api\.ui\.dialog\.replace/);
  assert.match(src, /api\.ui\.dialog\.clear/);
  assert.match(src, /api\.ui\.Dialog\(/);
  assert.match(src, /api\.ui\.toast\(/);
  assert.equal(src.includes("renderPopup"), false);

  // Esc closes the modal or opens the End Session confirm — it never ends the
  // session itself. Ending is only the explicit confirm action in the dialog.
  assert.match(src, /if \(getModal\(\)\) \{\s*closeModal\(\);\s*return;\s*\}\s*openModal\("exit"\)/);
  assert.match(src, /name === "enter" \|\| name === "return"[\s\S]{0,40}endSession\(\)/);
  assert.equal(/handleEscape[\s\S]{0,200}endSession\(\)/.test(src), false);

  assert.match(src, /setTranscript\(\[\]\)/);
  assert.match(src, /restorePromptFocus\(\)/);
  assert.match(src, /key:\s*"escape",\s*cmd:\s*"mortic\.escape"/);
  assert.match(src, /recordSmoke\("exit\.confirm\.open"/);
  assert.match(src, /recordSmoke\("exit\.confirmed"\)/);
  assert.match(src, /recordSmoke\("popup\.copy"/);
  assert.match(src, /recordSmoke\("modal\.open"/);
  assert.match(src, /recordSmoke\("modal\.close"/);
});

test("focusing without an open session is refused, not silently locked", () => {
  // sidebar_content only mounts on the session route; focusing anyway would
  // engage the typing lock against a sidepod that never renders anything.
  assert.match(src, /const focusMortic = \(\) => \{/);
  assert.match(src, /api\.route\.current\.name !== "session"[\s\S]{0,300}api\.ui\.toast\(/);
  assert.match(src, /recordSmoke\("focus\.blocked"/);
  // The blocked path must return before any mode push / focus lock.
  assert.match(
    src,
    /if \(api\.route\.current\.name !== "session"\) \{[\s\S]*?return;\s*\}\s*mutate\(\(\) => \{\s*if \(!exitMorticMode\)/
  );
});

test("command deck uses key-true labels and one PTT key", () => {
  for (const label of ["[M]", "[L]", "[X]", "[T]", "[H]", "[ESC]", "End Session"]) {
    assert.ok(src.includes(label), `deck label missing: ${label}`);
  }
  for (const stale of ["[PTT]", "[LIVE]", "[CLR]", "[TRN]", "[HND]"]) {
    assert.equal(src.includes(stale), false, `stale deck label present: ${stale}`);
  }
  // M is the only PTT key; p was dropped as a hidden alias.
  assert.equal(/key:\s*"p"/.test(src), false);
  assert.match(src, /key:\s*"x",\s*cmd:\s*"mortic\.clear"/);
  // Noisy status-only rows removed: sprite and row states carry status.
  assert.equal(/row\("focus"/.test(src), false);
  assert.equal(/row\("voice lane"/.test(src), false);
  assert.equal(/row\("last"/.test(src), false);
  assert.equal(/row\("items"/.test(src), false);
});

test("plain M toggle emits protocol v0 PTT controls", () => {
  assert.match(src, /MORTIC_HELPER_WS_URL/);
  assert.match(src, /new WebSocketCtor\(HELPER_WS_URL\)/);
  assert.match(src, /recordSmoke\("protocol\.send"/);
  assert.match(src, /protocolBase\("ptt\.start"\)/);
  assert.match(src, /inputMode:\s*"ptt"/);
  assert.match(src, /key:\s*"M"/);
  assert.match(src, /protocolBase\("ptt\.stop"\)/);
  assert.match(src, /matchingStartEventId:\s*activePttStartEventId/);
  assert.match(src, /reason:\s*"tap\.toggle"/);
  assert.match(src, /turnId:\s*activePttTurnId/);
  assert.match(src, /key:\s*"m",\s*cmd:\s*"mortic\.ptt\.press"/);
  assert.match(src, /mode:\s*"mortic\.sidepod"[\s\S]*mortic\.ptt\.press/);
  // The offline send queue is capped so stale PTT events are never replayed
  // in bulk when the helper reconnects.
  assert.match(src, /recordSmoke\("protocol\.drop"/);
});

test("slash registration matches OpenCode 1.17.x reachability rules", () => {
  // Slash menu requires a flat slashName on the layer command; the nested
  // legacy shape `slash: { name }` is only honored by deprecated api.command.
  assert.match(src, /slashName:\s*"mortic"/);
  assert.equal(/slash:\s*{/.test(src), false);
  assert.match(src, /name:\s*"mortic\.focus"[\s\S]*slashName:\s*"mortic"[\s\S]*run:\s*focusMortic/);
  assert.equal(/api\.(?:prompt|chat|message)\b/.test(src), false);

  // The palette layer must not be mode-pinned or the prompt's slash menu
  // treats its commands as unreachable. Only the focus-mode layer pins a mode.
  const modePins = src.match(/registerLayer\(\{\s*\n\s*mode:/g) ?? [];
  assert.equal(modePins.length, 1);
  assert.match(src, /mode:\s*"mortic\.sidepod"/);
});

test("normal UI source does not expose provider or runtime names", () => {
  for (const forbidden of ["Mercury", "mercury", "Inception", "Deepgram", "runtime"]) {
    assert.equal(src.includes(forbidden), false);
  }
});

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const rootDir = fileURLToPath(new URL("..", import.meta.url));
const pkg = JSON.parse(await readFile(join(rootDir, "package.json"), "utf8"));
const src = await readFile(join(rootDir, "src/tui.js"), "utf8");
const { orbLabel } = await import(join(rootDir, "src/tui.js"));

test("orb label reflects the live lane activity, not a constant", () => {
  assert.equal(orbLabel("thinking", true), "thinking");
  assert.equal(orbLabel("speaking", true), "speaking");
  assert.equal(orbLabel("connecting", true), "connecting");
  assert.equal(orbLabel("ready", true), "listening");
  assert.equal(orbLabel("ready", false), "muted");
  // Regression: the orb no longer stamps a hardcoded "thinking".
  assert.doesNotMatch(src, /overlayCentered\(rowText, "thinking"\)/);
});

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
  for (const expectedText of ["MORTIC", "COMMAND DECK", "COMMS", "Transcript", "Handoff", "Microphone"]) {
    assert.match(src, new RegExp(expectedText));
  }
});

test("source exposes MOR-165 slash and terminal smoke hooks", () => {
  assert.match(src, /renderer\.useKittyKeyboard/);
  assert.match(src, /\[mortic smoke\]/);
  assert.match(src, /name:\s*"mortic\.mic\.toggle"/);
});

test("focus mode locks typing and the mic is a plain M toggle", () => {
  // No hold-to-talk machinery: no keyrelease listener, no Kitty flag
  // changes, no repeat debounce, no event-type handling.
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

test("focus and typing-lock state render persistently beside the prompt", () => {
  // Owner correction 2026-07-03: a collapsed sidebar panel means the pod can
  // be invisible, so indication has to reach the user outside it. A toast
  // was tried first; session_prompt_right (verified live via PTY: content
  // renders directly beside the OpenCode prompt row) replaced it because a
  // reactive slot is simpler than a toast + one-shot-notice flag — it's
  // immutable while focused and clears itself the instant focus ends, with
  // no timer or manual flush.
  assert.match(src, /function renderPromptAnnex\(state, theme\)/);
  assert.match(src, /session_prompt_right:\s*\(\)\s*=>\s*renderPromptAnnex/);
  assert.match(src, /if \(!state\.focused\) {\s*return text\({}, \[""\]\);\s*}/);
  // No toast/one-shot-notice machinery left over from the earlier design.
  assert.equal(src.includes("noteSwallowedKey"), false);
  assert.equal(src.includes("swallowNoticeShown"), false);
  // The hero caption reflects focus/mic state instead of a static label.
  assert.match(src, /function heroCaption\(state\)/);
  assert.match(src, /MIC LIVE/);
  assert.match(src, /MIC MUTED/);
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
  assert.match(src, /recordSmoke\("modal\.copy"/);
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

test("command deck uses key-true labels and a single mic control", () => {
  for (const label of ["[M]", "[X]", "[T]", "[H]", "[ESC]", "End Session", "Microphone"]) {
    assert.ok(src.includes(label), `deck label missing: ${label}`);
  }
  for (const stale of ["[PTT]", "[LIVE]", "[L]", "[CLR]", "[TRN]", "[HND]", "Push to Talk"]) {
    assert.equal(src.includes(stale), false, `stale deck label present: ${stale}`);
  }
  // PTT and Live were collapsed into one mic toggle (owner decision
  // 2026-07-03): no separate live key/command, no p alias.
  assert.equal(/key:\s*"p"/.test(src), false);
  assert.equal(/key:\s*"l",\s*cmd:/.test(src), false);
  assert.equal(src.includes("mortic.live"), false);
  assert.equal(src.includes("toggleLive"), false);
  assert.match(src, /key:\s*"x",\s*cmd:\s*"mortic\.clear"/);
  // Noisy status-only rows removed: sprite and row states carry status.
  assert.equal(/row\("focus"/.test(src), false);
  assert.equal(/row\("voice lane"/.test(src), false);
  assert.equal(/row\("last"/.test(src), false);
  assert.equal(/row\("items"/.test(src), false);
});

test("the mic toggle emits protocol v0 live.set and drops PTT plumbing", () => {
  assert.match(src, /new WebSocketCtor\(helperWsUrl\(\)\)/);
  assert.match(src, /recordSmoke\("protocol\.send"/);
  assert.match(src, /const toggleMic = \(\) => \{/);
  assert.match(src, /protocolBase\("live\.set"\)/);
  assert.match(src, /value:\s*next/);
  assert.match(src, /reason:\s*"user\.toggle"/);
  assert.match(src, /recordSmoke\("mic\.state"/);
  assert.match(src, /key:\s*"m",\s*cmd:\s*"mortic\.mic\.toggle"/);
  assert.match(src, /mode:\s*"mortic\.sidepod"[\s\S]*mortic\.mic\.toggle/);
  // Hold-PTT plumbing is fully retired from the UI (still defined in the
  // protocol doc/engine for a possible future hold interaction).
  for (const stale of ["ptt.start", "ptt.stop", "handlePttKey", "activePttTurnId", "activePttStartEventId"]) {
    assert.equal(src.includes(stale), false, `stale PTT plumbing present: ${stale}`);
  }
  // The offline send queue is capped so stale events are never replayed
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

test("normal UI source does not expose provider or runtime names", async () => {
  // Case-insensitive and across every shipped source module — the earlier
  // capitalized-only check let a lowercase provider event name slip through
  // review on an external branch.
  const sources = [
    "src/tui.js",
    "src/index.js",
    "src/helper-launcher.mjs",
    "src/lane-reducer.mjs",
    "src/protocol-validate.mjs",
    "src/protocol.gen.mjs",
    "src/host-context.mjs"
  ];
  for (const file of sources) {
    const content = (await readFile(join(rootDir, file), "utf8")).toLowerCase();
    for (const forbidden of ["mercury", "inception", "deepgram", "runtime", "flux", "aura"]) {
      assert.equal(content.includes(forbidden), false, `${file} exposes: ${forbidden}`);
    }
  }
});

test("focus starts a confirmed managed voice lane for the focused thread", () => {
  // Focus is non-blocking: the helper is discovered/launched asynchronously
  // while the caption shows CONNECTING/OFFLINE instead of a silent wait.
  assert.match(src, /recordSmoke\("focus"\);\s*\}\);\s*startVoiceLane\(\)/);
  assert.match(src, /DialogConfirm/);
  assert.match(src, /Start Mortic Voice\?/);
  assert.match(src, /probeExistingHelper/);
  assert.match(src, /managedStartPromptOpen[\s\S]*?name === "enter" \|\| name === "return"[\s\S]*?acceptManagedStart\(\)/);
  assert.match(src, /managedConsentKey/);
  assert.match(src, /ensureHelper\(\{/);
  assert.match(src, /managed,\s*workspaceDir/);
  assert.match(src, /const failVoiceStartup = \(reason, \{ restoreFocus = false \} = \{\}\)/);
  assert.match(src, /if \(restoreFocus\) \{[\s\S]*?restorePromptFocus\(\)/);
  assert.match(src, /laneStatus === "offline"[\s\S]*?offlineToastShown = false/);
  assert.match(src, /failVoiceStartup\(result\.reason, \{ restoreFocus: true \}\)/);
  assert.match(src, /START_BLOCKING_ISSUES/);
  assert.match(src, /voice_lane_already_active/);
  assert.match(src, /voice_tmp_source_session/);
  assert.match(src, /restorePromptFocus\(\);[\s\S]*?recordSmoke\("lane\.start\.blocked"/);
  assert.match(src, /CONNECTING VOICE/);
  assert.match(src, /VOICE OFFLINE · M TO RETRY/);
  // start carries the focused thread's session id. The normal path uses the
  // helper's managed voice server; only an explicit env override supplies
  // start.opencodeUrl for dev attach.
  assert.match(src, /protocolBase\("start"\)/);
  assert.match(src, /sourceSessionId:\s*String\(sessionId\)/);
  assert.match(src, /keepFork:\s*false/);
  assert.match(src, /devOpencodeUrl/);
  assert.match(src, /start\.opencodeUrl\s*=/);
  assert.equal(src.includes("opencodeServerUrl"), false);
  assert.match(src, /protocolVersion:\s*PROTOCOL_VERSION/);
  // The helper is only ever launched from the focus path, never at load time.
  assert.equal(src.match(/ensureHelper\(/g).length, 1);
  assert.match(src, /const continueVoiceLaneStart = \(\{ opencodeUrl, managed, workspaceDir \}\) => \{[\s\S]*?ensureHelper\(\{/);
  assert.match(src, /const startVoiceLane = \(\{ confirmed = false \} = \{\}\) => \{/);
});

test("the lane client validates both directions against the shared schema", () => {
  assert.match(src, /checkMessage\("command", payload\)/);
  assert.match(src, /checkMessage\("event", payload\)/);
  assert.match(src, /recordSmoke\("protocol\.outbound\.invalid"/);
  assert.match(src, /recordSmoke\("protocol\.recv\.unknown"/);
  assert.match(src, /recordSmoke\("protocol\.recv\.invalid"/);
  assert.match(src, /reduceLaneEvent\(laneState, event\)/);
  // Reconnect backs off instead of hammering a dead helper.
  assert.match(src, /backoffMs = Math\.min\(backoffMs \* 2, 8000\)/);
});

test("end session sends stop and muting mid-reply barges in", () => {
  assert.match(src, /stopVoiceLane\("user\.end_session", \{ releaseHelper: true \}\)/);
  assert.match(src, /protocolBase\("stop"\)/);
  assert.match(src, /STOP_ACK_TIMEOUT_MS/);
  assert.match(src, /event\.type === "stopped" && stopAckTimer/);
  assert.match(src, /helper\.stop\.after_ack/);
  assert.match(src, /helper\.stop\.after_timeout/);
  assert.match(src, /helper\.stop\.immediate/);
  assert.match(src, /stopVoiceLane\("client\.shutdown", \{ releaseHelper: true, immediateHelperStop: true \}\)/);
  assert.match(src, /protocolBase\("barge_in"\)/);
  assert.match(src, /reason:\s*"user\.mute"/);
});

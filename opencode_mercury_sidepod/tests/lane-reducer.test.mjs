import assert from "node:assert/strict";
import { test } from "node:test";

import { createLaneState, reduceLaneEvent } from "../src/lane-reducer.mjs";

const at = "2026-07-04T00:00:00.000Z";

function play(events, state = createLaneState()) {
  const intents = [];
  for (const event of events) {
    const result = reduceLaneEvent(state, event);
    state = result.state;
    intents.push(result.ui);
  }
  return { state, intents };
}

test("a full turn drives user text, assistant buffer, and transcript", () => {
  const { state, intents } = play([
    { type: "ready", sentAt: at, voiceLaneId: "lane_1", state: "ready", forkSessionId: "fork_1" },
    { type: "listening", sentAt: at, voiceLaneId: "lane_1", mode: "live" },
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 1, text: "Make it", final: false },
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 2, text: "Make it scan.", final: true },
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live" },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 1, delta: "On it. " },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 2, delta: "Done." },
    { type: "speaking", sentAt: at, turnId: "turn_0001" },
    { type: "complete", sentAt: at, turnId: "turn_0001", latency: { totalMs: 4200 } }
  ]);

  assert.equal(intents[1].micLive, true);
  assert.equal(intents[2].userText, "Make it…");
  assert.equal(intents[3].userText, "Make it scan.");
  assert.deepEqual(intents[3].appendTranscript, [{ role: "user", text: "Make it scan." }]);
  assert.equal(intents[4].status, "thinking");
  assert.equal(intents[6].assistantText, "On it. Done.");
  assert.equal(intents[7].status, "speaking");
  assert.equal(intents[8].status, "ready");
  assert.deepEqual(intents[8].appendTranscript, [{ role: "assistant", text: "On it. Done." }]);
  assert.equal(intents[8].smoke.event, "lane.turn.complete");
  assert.equal(state.activeTurnId, null);
});

test("a poll-fallback turn with no deltas renders via fullSpokenText", () => {
  const { intents } = play([
    { type: "ready", sentAt: at, voiceLaneId: "lane_1", state: "ready", forkSessionId: "fork_1" },
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 1, text: "Summarize it.", final: true },
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live" },
    {
      type: "complete",
      sentAt: at,
      turnId: "turn_0001",
      latency: { totalMs: 5100 },
      streamSource: "poll_after_event",
      fullSpokenText: "Here is the summary.",
    },
  ]);

  assert.equal(intents[3].assistantText, "Here is the summary.");
  assert.deepEqual(intents[3].appendTranscript, [{ role: "assistant", text: "Here is the summary." }]);
});

test("context preparation phases use concise non-technical copy", () => {
  const { intents } = play([
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live", phase: "preparing_context" },
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live", phase: "continuing" },
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live", phase: "try_again" },
  ]);

  assert.equal(intents[0].assistantText, "Preparing context…");
  assert.equal(intents[1].assistantText, "Continuing…");
  assert.equal(intents[2].assistantText, "Try again.");
});

test("a straggler partial never steals the running turn", () => {
  // The frozen-viewer bug from run 20260704T140244Z: a partial transcript
  // with a fresh turnId arrived right after thinking; superseding on it made
  // the running turn's deltas and complete fail the turnId guard.
  const { intents } = play([
    { type: "transcript", sentAt: at, turnId: "turn_0005", sequence: 1, text: "why change this", final: true },
    { type: "thinking", sentAt: at, turnId: "turn_0005", sourceMode: "live" },
    { type: "transcript", sentAt: at, turnId: "turn_0006", sequence: 1, text: "why change this file", final: false },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0005", sequence: 1, delta: "The roadmap holds. " },
    { type: "complete", sentAt: at, turnId: "turn_0005", latency: { totalMs: 1600 } },
  ]);

  assert.equal(intents[2].userText, "why change this file…", "straggler still shows as a live caption");
  assert.equal(intents[3].assistantText, "The roadmap holds. ");
  assert.equal(intents[4].assistantText, "The roadmap holds. ");
  assert.deepEqual(intents[4].appendTranscript, [{ role: "assistant", text: "The roadmap holds. " }]);
});

test("interrupt clears the transcript sequence like complete does", () => {
  const { state, intents } = play([
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 4, text: "long dropped question", final: true },
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live" },
    { type: "interrupted", sentAt: at, turnId: "turn_0001", reason: "speech_confirmed" },
  ]);

  assert.equal(state.transcriptSeq, 0, "a stale high-water mark would silently drop the next turn's transcript");
  assert.equal(intents[2].userText, "Interrupted.");
  assert.equal(intents[2].assistantText, "Speech interrupted before playback finished.");
});

test("stale and duplicate sequences never regress state", () => {
  const { intents } = play([
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 3, text: "newer", final: false },
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 2, text: "older", final: false },
    { type: "transcript", sentAt: at, turnId: "turn_0001", sequence: 3, text: "duplicate", final: false },
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live" },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 2, delta: "b" },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 1, delta: "a" },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 2, delta: "dup" }
  ]);

  assert.equal(intents[1], null);
  assert.equal(intents[2], null);
  assert.equal(intents[4].assistantText, "b");
  assert.equal(intents[5], null);
  assert.equal(intents[6], null);
});

test("events for superseded turns do not replace the active display", () => {
  const { intents } = play([
    { type: "thinking", sentAt: at, turnId: "turn_0002", sourceMode: "live" },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 9, delta: "stale turn" },
    { type: "speaking", sentAt: at, turnId: "turn_0001" },
    { type: "complete", sentAt: at, turnId: "turn_0001", latency: { totalMs: 1 } },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0002", sequence: 1, delta: "live turn" }
  ]);

  assert.equal(intents[1], null);
  assert.equal(intents[2], null);
  assert.equal(intents[3], null, "a complete for a superseded turn must not clear the active turn");
  assert.equal(intents[4].assistantText, "live turn");
});

test("only a final transcript supersedes a running turn", () => {
  const { state } = play([
    { type: "thinking", sentAt: at, turnId: "turn_0001", sourceMode: "live" },
    { type: "assistant.delta", sentAt: at, turnId: "turn_0001", sequence: 1, delta: "old reply" },
    { type: "transcript", sentAt: at, turnId: "turn_0002", sequence: 1, text: "next ask", final: false }
  ]);

  // The partial shows as a caption but must not steal the running turn.
  assert.equal(state.activeTurnId, "turn_0001");
  assert.equal(state.assistantBuffer, "old reply");

  const { state: after } = play(
    [{ type: "transcript", sentAt: at, turnId: "turn_0002", sequence: 2, text: "next ask", final: true }],
    state
  );
  assert.equal(after.activeTurnId, "turn_0002");
  assert.equal(after.assistantBuffer, "");
});

test("audio-capability issues flip the mic back to muted, others do not", () => {
  const audio = reduceLaneEvent(createLaneState(), {
    type: "voice_bridge_issue",
    sentAt: at,
    userMessage: "Voice Bridge Issue",
    safeDetail: "Mic permission needed",
    diagnosticCode: "mic_permission_needed",
    capability: "voice_audio",
    retryable: true
  });
  assert.equal(audio.ui.micLive, false);
  assert.equal(audio.ui.toast.variant, "error");
  assert.match(audio.ui.toast.message, /Mic permission needed/);

  const turn = reduceLaneEvent(createLaneState(), {
    type: "voice_bridge_issue",
    sentAt: at,
    userMessage: "Voice Bridge Issue",
    diagnosticCode: "turn_failed",
    capability: "voice_turns",
    retryable: true
  });
  assert.equal("micLive" in turn.ui, false);
});

test("stopped resets the lane and unknown events are ignored", () => {
  const stopped = reduceLaneEvent(
    { ...createLaneState(), activeTurnId: "turn_0001" },
    { type: "stopped", sentAt: at, reason: "user.end_session", forkDeleted: true }
  );
  assert.equal(stopped.state.activeTurnId, null);
  assert.equal(stopped.ui.micLive, false);
  assert.equal(stopped.ui.status, "ended");

  const unknown = reduceLaneEvent(createLaneState(), { type: "speech.telemetry", sentAt: at });
  assert.equal(unknown.ui, null);
});

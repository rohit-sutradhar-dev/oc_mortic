// Pure reducer from protocol v0 engine events to sidepod UI intents.
//
// tui.js owns the Solid signals; this module owns the contract semantics the
// protocol doc requires of renderers: events for superseded turnIds must not
// replace the active turn display, and stale/duplicate sequence values must
// not regress state. Pure function -> exhaustively unit-testable.

export function createLaneState() {
  return {
    activeTurnId: null,
    transcriptSeq: 0,
    deltaSeq: 0,
    assistantBuffer: "",
  };
}

// The engine tags every issue payload with the capability it degrades
// (voice_bridge_issue_payload in the helper); audio problems mute the mic.
function isAudioIssue(event) {
  return event.capability === "voice_audio";
}

/**
 * Returns { state, ui } where ui is null for ignored events, else an intent:
 * { userText?, assistantText?, appendTranscript?: {role,text}[], micLive?,
 *   status?, toast?: {variant,message}, latency?, smoke?: {event,details} }.
 */
export function reduceLaneEvent(state, event) {
  const type = event?.type;
  if (type === "ready") {
    return {
      state: createLaneState(),
      ui: {
        status: "ready",
        assistantText: "Voice lane ready. Tap M to talk.",
        appendTranscript: [{ role: "system", text: "Voice lane ready." }],
        smoke: { event: "lane.ready", details: { forkSessionId: event.forkSessionId } },
      },
    };
  }
  if (type === "listening") {
    return {
      state,
      ui: { status: "ready", micLive: true, userText: "Listening. Speak normally." },
    };
  }
  if (type === "transcript") {
    if (event.turnId === state.activeTurnId) {
      if (event.sequence <= state.transcriptSeq) {
        return { state, ui: null };
      }
      const next = { ...state, transcriptSeq: event.sequence };
      return applyTranscript(next, event);
    }
    // A transcript for a new turnId supersedes the active turn display.
    const next = {
      ...state,
      activeTurnId: event.turnId,
      transcriptSeq: event.sequence,
      deltaSeq: 0,
      assistantBuffer: "",
    };
    return applyTranscript(next, event);
  }
  if (type === "thinking") {
    if (state.activeTurnId && event.turnId !== state.activeTurnId) {
      return { state, ui: null };
    }
    return {
      state: { ...state, activeTurnId: event.turnId, deltaSeq: 0, assistantBuffer: "" },
      ui: { status: "thinking", assistantText: "…" },
    };
  }
  if (type === "assistant.delta") {
    if (event.turnId !== state.activeTurnId || event.sequence <= state.deltaSeq) {
      return { state, ui: null };
    }
    const assistantBuffer = state.assistantBuffer + String(event.delta ?? "");
    return {
      state: { ...state, deltaSeq: event.sequence, assistantBuffer },
      ui: { assistantText: assistantBuffer, status: "thinking" },
    };
  }
  if (type === "speaking") {
    if (event.turnId !== state.activeTurnId) {
      return { state, ui: null };
    }
    return { state, ui: { status: "speaking" } };
  }
  if (type === "complete") {
    if (state.activeTurnId && event.turnId !== state.activeTurnId) {
      return { state, ui: null };
    }
    const finalText = state.assistantBuffer || String(event.fullSpokenText ?? "");
    return {
      state: { ...state, activeTurnId: null, assistantBuffer: "", deltaSeq: 0, transcriptSeq: 0 },
      ui: {
        status: "ready",
        ...(finalText ? { assistantText: finalText, appendTranscript: [{ role: "assistant", text: finalText }] } : {}),
        smoke: { event: "lane.turn.complete", details: { turnId: event.turnId, latency: event.latency } },
      },
    };
  }
  if (type === "interrupted") {
    return {
      state: { ...state, activeTurnId: null, assistantBuffer: "", deltaSeq: 0 },
      ui: { status: "ready", userText: "Interrupted." },
    };
  }
  if (type === "voice_bridge_issue") {
    const detail = event.safeDetail ? `${event.userMessage}: ${event.safeDetail}` : String(event.userMessage ?? "");
    return {
      state,
      ui: {
        status: "ready",
        assistantText: detail,
        toast: { variant: "error", message: detail },
        ...(isAudioIssue(event) ? { micLive: false } : {}),
        smoke: { event: "lane.issue", details: { diagnosticCode: event.diagnosticCode } },
      },
    };
  }
  if (type === "stopped") {
    return {
      state: createLaneState(),
      ui: { status: "ended", micLive: false, smoke: { event: "lane.stopped", details: { reason: event.reason } } },
    };
  }
  return { state, ui: null };
}

function applyTranscript(state, event) {
  const text = String(event.text ?? "");
  if (event.final) {
    return {
      state,
      ui: { userText: text, appendTranscript: [{ role: "user", text }] },
    };
  }
  return { state, ui: { userText: `${text}…` } };
}

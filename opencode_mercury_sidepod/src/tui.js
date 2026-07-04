import { createElement, insert, setProp } from "@opentui/solid";
import { createMemo, createSignal } from "solid-js";

import { ensureHelper, helperWsUrl, stopHelper } from "./helper-launcher.mjs";
import { createLaneState, reduceLaneEvent } from "./lane-reducer.mjs";
import { checkMessage } from "./protocol-validate.mjs";
import { PROTOCOL_VERSION } from "./protocol.gen.mjs";
import { appendSmoke, opencodeServerUrl } from "./host-context.mjs";

const WIDTH = 36;
const INNER = WIDTH - 2;
// Centered host dialogs are wider than the sidepod column.
const MODAL_INNER = 60;
const MODAL_PAGE = 14;
const SMOKE_SINK = globalThis.process?.env?.MORTIC_SMOKE_LOG;
const STOP_ACK_TIMEOUT_MS = 2000;

function element(type, props = {}, children = []) {
  const node = createElement(type);
  for (const [key, value] of Object.entries(props)) {
    if (value !== undefined && value !== null) {
      setProp(node, key, value);
    }
  }
  for (const child of children) {
    if (child !== null && child !== undefined && child !== false) {
      insert(node, child);
    }
  }
  return node;
}

function box(props, children = []) {
  return element("box", props, children);
}

function text(props, children) {
  return element("text", props, children);
}

function fit(value, width) {
  const clean = String(value).replace(/[\r\n\t]/g, " ");
  if (clean.length <= width) {
    return clean.padEnd(width, " ");
  }
  return `${clean.slice(0, Math.max(0, width - 1))}…`;
}

function line(value, color) {
  return text({ fg: color }, [fit(value, WIDTH)]);
}

function clickable(value, color, onMouseDown) {
  return box({ width: "100%", onMouseDown }, [line(value, color)]);
}

function center(value, width = INNER) {
  const clean = String(value);
  if (clean.length >= width) {
    return fit(clean, width);
  }
  const left = Math.floor((width - clean.length) / 2);
  return `${" ".repeat(left)}${clean}${" ".repeat(width - clean.length - left)}`;
}

function wrap(value, width = INNER) {
  const words = String(value).split(/\s+/).filter(Boolean);
  const lines = [];
  let current = "";
  for (const word of words) {
    if (!current) {
      current = word;
    } else if (`${current} ${word}`.length <= width) {
      current = `${current} ${word}`;
    } else {
      lines.push(current);
      current = word;
    }
  }
  if (current) {
    lines.push(current);
  }
  return lines.length ? lines : [""];
}

function frame(title, children, color, borderColor, style = "heavy") {
  const chars =
    style === "soft"
      ? { tl: "╭", tr: "╮", bl: "╰", br: "╯", h: "─", v: "│" }
      : { tl: "╔", tr: "╗", bl: "╚", br: "╝", h: "═", v: "║" };
  const label = title ? ` ${title} ` : "";
  const topFill = chars.h.repeat(Math.max(0, WIDTH - 2 - label.length));
  const lines = [{ text: `${chars.tl}${label}${topFill}${chars.tr}`, color: borderColor }];
  for (const child of children) {
    const childText = typeof child === "string" ? child : child.text;
    const childColor = typeof child === "string" ? color : child.color ?? color;
    lines.push({ text: `${chars.v}${fit(childText, INNER)}${chars.v}`, color: childColor });
  }
  lines.push({ text: `${chars.bl}${chars.h.repeat(WIDTH - 2)}${chars.br}`, color: borderColor });
  return lines.map((item) => line(item.text, item.color));
}

function renderBrailleOrb(phase, active, captionColor) {
  const cols = 12;
  const rows = 6;
  const subWidth = cols * 2;
  const subHeight = rows * 4;
  const cx = (subWidth - 1) / 2;
  const cy = (subHeight - 1) / 2;
  const radius = active ? 8.95 : 8.35;
  const haloRadius = active ? radius + 1.25 : radius;
  const output = [];

  for (let cellY = 0; cellY < rows; cellY += 1) {
    let rowText = "";
    for (let cellX = 0; cellX < cols; cellX += 1) {
      let bits = 0;
      for (let localY = 0; localY < 4; localY += 1) {
        for (let localX = 0; localX < 2; localX += 1) {
          const x = cellX * 2 + localX;
          const y = cellY * 4 + localY;
          const dx = x - cx;
          const dy = y - cy;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const stableShell = dist <= radius;
          const protectedEdge = dist > radius - 0.9 && stableShell;
          const airPocket = active && dist < radius - 1.9 && texture(x, y, phase) < 0.03;
          const halo = active && dist > radius && dist <= haloRadius && radiates(x, y, phase, dist, radius, cx, cy);
          if ((stableShell && (protectedEdge || !airPocket)) || halo) {
            bits |= BRAILLE_BITS[localY][localX];
          }
        }
      }
      rowText += bits ? String.fromCharCode(0x2800 + bits) : " ";
    }
    output.push(orbCaption(rowText, cellY, active, captionColor));
  }
  return output;
}

function orbCaption(rowText, rowIndex, active, captionColor) {
  if (!active) {
    return rowText;
  }
  if (rowIndex === 2) {
    return { text: overlayCentered(rowText, "thinking"), color: captionColor };
  }
  return rowText;
}

function overlayCentered(base, label) {
  const start = Math.max(0, Math.floor((base.length - label.length) / 2));
  return `${base.slice(0, start)}${label}${base.slice(start + label.length)}`;
}

const BRAILLE_BITS = [
  [0x01, 0x08],
  [0x02, 0x10],
  [0x04, 0x20],
  [0x40, 0x80]
];

function texture(x, y, phase) {
  return ((x * 53 + y * 31 + phase * 17 + ((x * y) % 29)) % 1000) / 1000;
}

function radiates(x, y, phase, dist, radius, cx, cy) {
  const angle = Math.atan2(y - cy, x - cx);
  const pulse = Math.sin((phase / 8) * Math.PI * 2);
  const ring = Math.abs(dist - (radius + 0.72 + pulse * 0.24)) < 0.26;
  const ray = Math.cos(angle * 16 - phase * 0.85) > 0.76 && dist < radius + 1.55;
  return ring || ray;
}

function commandRow(key, label, status, color, onMouseDown) {
  return clickable(`║ ${fit(key, 6)} ${fit(label, 15)} ${fit(status, 9)} ║`, color, onMouseDown);
}

// One state bit is the entire voice control surface: mic muted or mic live.
// Turn segmentation is the engine's job (native end-of-turn detection lives
// server-side); the UI only gates whether the mic may listen. Lane transport
// state outranks mic state in the caption so a dead engine is never silent.
function heroCaption(state) {
  if (!state.focused) {
    return "/MORTIC TO FOCUS";
  }
  if (state.laneStatus === "offline") {
    return "VOICE OFFLINE · M TO RETRY";
  }
  if (state.laneStatus === "connecting") {
    return "CONNECTING VOICE…";
  }
  return state.micLive ? "MIC LIVE · M TO MUTE" : "MIC MUTED · M TO TALK";
}

function renderHero(state, theme) {
  // TuiThemeCurrent declares these colors non-optional; no fallbacks needed.
  const accent = theme.accent;
  const muted = theme.textMuted;
  const secondaryAccent = theme.secondary;
  const ok = theme.success;
  const active = state.micLive;
  const color = active ? ok : muted;
  const border = state.focused ? secondaryAccent : accent;
  return [
    ...frame(
      "MORTIC",
      [
        center("M O R T I C", INNER),
        center("", INNER),
        ...renderBrailleOrb(state.phase, active, secondaryAccent).map((item) =>
          typeof item === "string"
            ? center(item, INNER)
            : { text: center(item.text, INNER), color: item.color }
        ),
        center("", INNER),
        center(heroCaption(state), INNER)
      ],
      color,
      border,
      "heavy"
    )
  ];
}

function renderControlPanel(state, actions, theme) {
  const accent = theme.accent;
  const muted = theme.textMuted;
  const micColor = state.micLive ? theme.success : muted;
  return [
    line("", muted),
    line(`╔ COMMAND DECK${"═".repeat(WIDTH - 15)}╗`, accent),
    commandRow("[M]", "Microphone", state.micLive ? "LIVE" : "MUTED", micColor, actions.toggleMic),
    commandRow("[X]", "Clear Lane", "", theme.warning, actions.clear),
    commandRow("[T]", "Transcript", "", accent, actions.openTranscript),
    commandRow("[H]", "Handoff", state.handoffReady ? "READY" : "DRAFT", accent, actions.openHandoff),
    commandRow("[ESC]", "End Session", "", theme.error, actions.requestEnd),
    line(`╚${"═".repeat(WIDTH - 2)}╝`, accent)
  ];
}

function renderConversation(state, theme) {
  const accent = theme.accent;
  const muted = theme.textMuted;
  const userLines = wrap(state.userText, INNER);
  const assistantLines = wrap(state.assistantText, INNER);
  return [
    line("", muted),
    ...frame(
      "COMMS",
      [
        "YOU",
        ...userLines,
        " ",
        "MORTIC",
        ...assistantLines
      ],
      theme.text,
      accent,
      "soft"
    )
  ];
}

function transcriptText(transcript) {
  return transcript.map((item) => `${item.role}: ${item.text}`).join("\n");
}

function handoffText(userText, assistantText) {
  return [
    "Mortic handoff draft",
    "",
    `User intent: ${userText}`,
    `Assistant result: ${assistantText}`,
    "",
    "Keep code, commands, paths, diffs, and JSON screen-only."
  ].join("\n");
}

// The file sink is always on (appendSmoke defaults it) so a live viewer bug
// leaves a client-side trace without the owner pre-arming an env var. The
// console echo stays opt-in: console output in a raw TUI is painted over by
// redraws and can corrupt the frame, so only emit it under MORTIC_SMOKE_LOG.
function logSmoke(api, event, details = {}) {
  const payload = {
    event,
    mode: api.mode.current?.(),
    useKittyKeyboard: Boolean(api.renderer.useKittyKeyboard),
    at: new Date().toISOString(),
    ...details
  };
  if (SMOKE_SINK) {
    console.info("[mortic smoke]", JSON.stringify(payload));
  }
  appendSmoke(payload);
}

// Voice-lane protocol client. Socket lifetime = lane lifetime: opened on
// focus, closed after an acknowledged stop (or its timeout). Both directions
// are validated against the generated v0 schema — off-contract outbound is
// dropped before it reaches the wire, off-contract inbound never reaches the
// reducer, and unknown inbound types are logged and ignored per the
// compatibility rules.
function createLaneClient({ recordSmoke, onEvent, onOpen, onDown }) {
  const WebSocketCtor = globalThis.WebSocket;
  let socket;
  let closedByUs = true;
  let backoffMs = 500;
  let reconnectTimer;
  const queue = [];
  const flush = () => {
    while (socket?.readyState === 1 && queue.length) {
      socket.send(queue.shift());
    }
  };
  const scheduleReconnect = () => {
    if (closedByUs || reconnectTimer) {
      return;
    }
    reconnectTimer = setTimeout(() => {
      reconnectTimer = undefined;
      dial();
    }, backoffMs);
    backoffMs = Math.min(backoffMs * 2, 8000);
  };
  const dial = () => {
    if (closedByUs) {
      return;
    }
    if (!WebSocketCtor) {
      recordSmoke("protocol.unavailable", { reason: "websocket-missing" });
      return;
    }
    if (socket && (socket.readyState === 0 || socket.readyState === 1)) {
      return;
    }
    try {
      socket = new WebSocketCtor(helperWsUrl());
      socket.onopen = () => {
        backoffMs = 500;
        flush();
        onOpen();
      };
      socket.onmessage = (message) => {
        let payload;
        try {
          payload = JSON.parse(message.data);
        } catch {
          recordSmoke("protocol.recv.invalid", { reason: "bad-json" });
          return;
        }
        const check = checkMessage("event", payload);
        if (check.ok) {
          recordSmoke("protocol.recv", { type: payload.type });
          onEvent(payload);
        } else if (check.unknownType) {
          recordSmoke("protocol.recv.unknown", { type: payload.type });
        } else {
          recordSmoke("protocol.recv.invalid", { type: payload.type, errors: check.errors });
        }
      };
      socket.onclose = () => {
        socket = undefined;
        if (!closedByUs) {
          onDown();
          scheduleReconnect();
        }
      };
      socket.onerror = () => {
        recordSmoke("protocol.unavailable", { reason: "websocket-error" });
      };
    } catch {
      recordSmoke("protocol.unavailable", { reason: "websocket-open-failed" });
      scheduleReconnect();
    }
  };
  return {
    connect() {
      closedByUs = false;
      backoffMs = 500;
      dial();
    },
    send(payload) {
      const check = checkMessage("command", payload);
      if (!check.ok) {
        recordSmoke("protocol.outbound.invalid", { type: payload?.type, errors: check.errors });
        return false;
      }
      const serialized = JSON.stringify(payload);
      recordSmoke("protocol.send", { type: payload.type });
      if (socket?.readyState === 1) {
        socket.send(serialized);
        return true;
      }
      // Never replay a long backlog of stale queued events when the helper
      // reconnects: keep only a short queue and drop the oldest beyond it.
      queue.push(serialized);
      if (queue.length > 16) {
        queue.shift();
        recordSmoke("protocol.drop", { reason: "queue-full" });
      }
      dial();
      return true;
    },
    close() {
      closedByUs = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = undefined;
      }
      queue.length = 0;
      try {
        socket?.close();
      } catch {
        // closing a dead socket is fine
      }
      socket = undefined;
    },
    isConnected: () => socket?.readyState === 1
  };
}

// Sits beside the OpenCode prompt row (session_prompt_right slot) so focus
// state and the typing lock are visible right where the user is about to
// type — persistent while focused, and reactively cleared the instant focus
// state flips false. No timer, no manual clear: the slot re-renders off the
// same signals sidebar_content uses.
function renderPromptAnnex(state, theme) {
  if (!state.focused) {
    return text({}, [""]);
  }
  let label = state.micLive ? "MIC LIVE" : "MIC MUTED";
  let color = state.micLive ? theme.success : theme.textMuted;
  if (state.laneStatus === "offline") {
    label = "VOICE OFFLINE";
    color = theme.error;
  } else if (state.laneStatus === "connecting") {
    label = "CONNECTING";
    color = theme.warning;
  }
  return text({ fg: color }, [`MORTIC · ${label} — Esc exit`]);
}

function renderPod(state, actions, theme) {
  return box(
    {
      width: "100%",
      flexDirection: "column",
      paddingTop: 0,
      paddingBottom: 0,
      paddingLeft: 0,
      paddingRight: 0
    },
    [
      ...renderHero(state, theme),
      ...renderControlPanel(state, actions, theme),
      ...renderConversation(state, theme)
    ]
  );
}

const id = "mortic.sidepod";

export async function tui(api) {
    const [getMicLive, setMicLive] = createSignal(false);
    const [getFocused, setFocused] = createSignal(false);
    // idle -> connecting -> ready/thinking/speaking, or offline when the
    // helper cannot be reached. Drives the hero caption and prompt annex.
    const [getLaneStatus, setLaneStatus] = createSignal("idle");
    const [getModal, setModal] = createSignal(null);
    const [getModalScroll, setModalScroll] = createSignal(0);
    const [getPhase, setPhase] = createSignal(0);
    const [getUserText, setUserText] = createSignal("Press M to unmute. Spoken asks will appear here.");
    const [getAssistantText, setAssistantText] = createSignal("Mortic replies with short speakable text. Details stay screen-only.");
    const [getTranscript, setTranscript] = createSignal([
      { role: "system", text: "Mortic sidepod loaded." },
      { role: "assistant", text: "Ready. Tap M to unmute." }
    ]);

    const requestRender = () => api.renderer.requestRender();
    // The orb animation ticks only while the mic is live; idle sessions run
    // no timer at all.
    let animationTimer;
    const syncAnimation = () => {
      const active = getMicLive();
      if (active && !animationTimer) {
        animationTimer = setInterval(() => {
          setPhase((getPhase() + 1) % 8);
          requestRender();
        }, 135);
      } else if (!active && animationTimer) {
        clearInterval(animationTimer);
        animationTimer = undefined;
      }
    };
    const mutate = (fn) => {
      fn();
      syncAnimation();
      requestRender();
    };
    const appendTranscript = (role, value) => {
      setTranscript([...getTranscript(), { role, text: value }].slice(-12));
    };
    let exitMorticMode;
    let previousFocus;
    let clientEventSeq = 0;

    const restorePromptFocus = () => {
      if (exitMorticMode) {
        exitMorticMode();
        exitMorticMode = undefined;
      }
      previousFocus?.focus?.();
      previousFocus = undefined;
      setFocused(false);
    };

    const focusMortic = () => {
      // sidebar_content only mounts on the session route (it requires a
      // session_id). Focusing without a session would lock the keyboard
      // against a sidepod that never renders — refuse and say why instead.
      if (api.route.current.name !== "session") {
        recordSmoke("focus.blocked", { reason: "no-session" });
        api.ui.toast({
          variant: "error",
          message: "Mortic needs an open chat session. Start one first."
        });
        return;
      }
      mutate(() => {
        if (!exitMorticMode) {
          exitMorticMode = api.mode.push("mortic.sidepod");
        }
        // The prompt input keeps renderable focus after /mortic, so blur it
        // for the duration of Mortic focus mode and restore it on exit.
        if (!previousFocus) {
          previousFocus = api.renderer.currentFocusedRenderable ?? undefined;
          previousFocus?.blur?.();
        }
        setFocused(true);
        recordSmoke("focus");
      });
      startVoiceLane();
    };
    const recordSmoke = (event, details = {}) => {
      logSmoke(api, event, details);
    };

    // Modals render as centered host dialogs over the whole TUI (never inside
    // the sidepod). Owner spec 2026-07-03: Esc is never destructive; ending a
    // session is an explicit confirm action inside the End Session dialog.
    const MODAL_TITLES = { transcript: "TRANSCRIPT", handoff: "HANDOFF", exit: "END SESSION" };
    const MODAL_FOOTERS = {
      transcript: "j/k scroll · c copy · esc close",
      handoff: "c copy · esc close",
      exit: "enter end session · h handoff · esc cancel"
    };
    const modalBodyLines = () => {
      const kind = getModal();
      if (kind === "transcript") {
        const items = getTranscript();
        const all = items.length
          ? items.flatMap((item) => wrap(`${item.role}: ${item.text}`, MODAL_INNER))
          : ["No transcript yet.", "Tap M to unmute and start."];
        const top = Math.min(getModalScroll(), Math.max(0, all.length - MODAL_PAGE));
        return all.slice(top, top + MODAL_PAGE);
      }
      if (kind === "handoff") {
        return handoffText(getUserText(), getAssistantText())
          .split("\n")
          .flatMap((item) => (item ? wrap(item, MODAL_INNER) : [""]));
      }
      if (kind === "exit") {
        return [
          "This voice lane is ephemeral.",
          "Ending clears the transcript and returns focus to the prompt.",
          "Open Handoff first if you want a copy."
        ].flatMap((item) => wrap(item, MODAL_INNER));
      }
      return [];
    };
    const onHostClose = () =>
      mutate(() => {
        if (getModal()) {
          recordSmoke("modal.close", { modal: getModal() });
          setModal(null);
        }
      });
    const renderModalContent = () => {
      const kind = getModal();
      const theme = api.theme.current;
      return api.ui.Dialog({
        size: "medium",
        onClose: onHostClose,
        children: box({ flexDirection: "column", paddingLeft: 1, paddingRight: 1 }, [
          text({ fg: theme.accent }, [MODAL_TITLES[kind] ?? ""]),
          text({ fg: theme.text }, [""]),
          ...modalBodyLines().map((item) => text({ fg: theme.text }, [item])),
          text({ fg: theme.text }, [""]),
          text({ fg: theme.textMuted }, [MODAL_FOOTERS[kind] ?? ""])
        ])
      });
    };
    const openModal = (kind) =>
      mutate(() => {
        setModal(kind);
        setModalScroll(0);
        if (kind === "exit") {
          recordSmoke("exit.confirm.open");
        } else {
          recordSmoke("modal.open", { modal: kind });
        }
        api.ui.dialog.replace(renderModalContent, onHostClose);
      });
    const closeModal = () =>
      mutate(() => {
        if (!getModal()) {
          return;
        }
        recordSmoke("modal.close", { modal: getModal() });
        setModal(null);
        api.ui.dialog.clear();
      });
    const refreshModal = () => {
      if (getModal()) {
        api.ui.dialog.replace(renderModalContent, onHostClose);
      }
    };
    const scrollModal = (delta) =>
      mutate(() => {
        setModalScroll(Math.max(0, getModalScroll() + delta));
        refreshModal();
      });
    const modalCopyText = () => {
      if (getModal() === "transcript") {
        return transcriptText(getTranscript());
      }
      if (getModal() === "handoff") {
        return handoffText(getUserText(), getAssistantText());
      }
      return "";
    };
    const copyModal = () =>
      mutate(() => {
        const value = modalCopyText();
        if (!value) {
          return;
        }
        recordSmoke("modal.copy", { modal: getModal() });
        // OSC 52 works in any terminal, including over SSH.
        const ok = api.renderer.copyToClipboardOSC52?.(value);
        api.ui.toast({ variant: ok ? "success" : "error", message: ok ? "Copied" : "Copy unavailable" });
      });
    const endSession = () =>
      mutate(() => {
        recordSmoke("exit.confirmed");
        setModal(null);
        api.ui.dialog.clear();
        // Tell the engine to tear the lane down (stop -> stopped ack closes
        // the socket; a 2s timeout closes it regardless). UI flush is local
        // and immediate either way.
        stopVoiceLane("user.end_session");
        setMicLive(false);
        setUserText("Mortic session ended.");
        setAssistantText("Start Mortic again for a fresh voice lane.");
        setTranscript([]);
        restorePromptFocus();
        api.ui.toast({ variant: "success", message: "Mortic session ended" });
      });
    // Esc is never destructive: inside a modal it closes the modal; outside it
    // opens the End Session confirmation. Only the explicit confirm action in
    // that dialog ends the session.
    const handleEscape = () => {
      if (getModal()) {
        closeModal();
        return;
      }
      openModal("exit");
    };
    const requestEnd = () => {
      if (!getModal()) {
        openModal("exit");
      }
    };
    const clearLane = () => {
      if (getModal()) {
        return;
      }
      mutate(() => {
        setMicLive(false);
        setUserText("Voice lane cleared.");
        setAssistantText("Ready for the next spoken turn.");
        setTranscript([{ role: "system", text: "Voice lane cleared." }]);
      });
    };
    const openTranscript = () => {
      if (getModal()) {
        return;
      }
      openModal("transcript");
    };
    const openHandoff = () => {
      // Reachable from idle focus mode and from the End Session dialog.
      if (getModal() && getModal() !== "exit") {
        return;
      }
      openModal("handoff");
    };
    const nextClientEventId = () => `evt_sidepod_${Date.now().toString(36)}_${++clientEventSeq}`;
    const protocolBase = (type) => ({
      type,
      clientEventId: nextClientEventId(),
      sentAt: new Date().toISOString()
    });

    // --- voice lane ---------------------------------------------------------
    let laneState = createLaneState();
    let stopAckTimer;
    let offlineToastShown = false;

    const applyLaneUi = (ui) => {
      if (!ui) {
        return;
      }
      mutate(() => {
        if (ui.status) {
          setLaneStatus(ui.status === "ended" ? "idle" : ui.status);
        }
        if (typeof ui.micLive === "boolean") {
          setMicLive(ui.micLive);
        }
        if (ui.userText !== undefined) {
          setUserText(ui.userText);
        }
        if (ui.assistantText !== undefined) {
          setAssistantText(ui.assistantText);
        }
        for (const item of ui.appendTranscript ?? []) {
          appendTranscript(item.role, item.text);
        }
        if (ui.toast) {
          api.ui.toast(ui.toast);
        }
        if (ui.smoke) {
          recordSmoke(ui.smoke.event, ui.smoke.details ?? {});
        }
      });
    };

    const laneClient = createLaneClient({
      recordSmoke,
      onEvent: (event) => {
        const result = reduceLaneEvent(laneState, event);
        laneState = result.state;
        if (event.type === "stopped" && stopAckTimer) {
          clearTimeout(stopAckTimer);
          stopAckTimer = undefined;
          laneClient.close();
        }
        applyLaneUi(result.ui);
      },
      onOpen: () => sendStart(),
      onDown: () => {
        if (getFocused()) {
          mutate(() => setLaneStatus("connecting"));
        }
      }
    });

    // The sidepod converses over the thread it was focused from: start carries
    // that thread's session id, and opencodeUrl pins the engine to the server
    // that owns it (recorded by the hook entry, env fallback for dev).
    const sourceSessionId = () => {
      const params = api.route.current?.params ?? {};
      return params.sessionID ?? params.sessionId ?? params.session_id ?? params.id;
    };
    const sendStart = () => {
      const sessionId = sourceSessionId();
      if (!sessionId) {
        return;
      }
      const start = {
        ...protocolBase("start"),
        protocolVersion: PROTOCOL_VERSION,
        sourceSessionId: String(sessionId),
        keepFork: false
      };
      const opencodeUrl = opencodeServerUrl();
      if (opencodeUrl) {
        start.opencodeUrl = String(opencodeUrl);
      }
      laneClient.send(start);
    };

    // Non-blocking: focus proceeds immediately while the helper is discovered
    // or launched; the caption shows CONNECTING/OFFLINE instead of a silent
    // wait, and M retries from the offline state.
    const startVoiceLane = () => {
      if (getLaneStatus() === "connecting") {
        return;
      }
      mutate(() => setLaneStatus("connecting"));
      ensureHelper({ opencodeUrl: opencodeServerUrl(), log: recordSmoke })
        .then((result) => {
          if (!result.ready) {
            mutate(() => setLaneStatus("offline"));
            if (!offlineToastShown) {
              offlineToastShown = true;
              api.ui.toast({ variant: "error", message: "Voice engine offline. Tap M to retry." });
            }
            return;
          }
          offlineToastShown = false;
          laneClient.connect();
          if (laneClient.isConnected()) {
            sendStart();
          }
        })
        .catch(() => mutate(() => setLaneStatus("offline")));
    };

    const stopVoiceLane = (reason) => {
      laneState = createLaneState();
      if (laneClient.isConnected()) {
        laneClient.send({ ...protocolBase("stop"), reason });
        stopAckTimer = setTimeout(() => {
          stopAckTimer = undefined;
          laneClient.close();
        }, STOP_ACK_TIMEOUT_MS);
      } else {
        laneClient.close();
      }
      setLaneStatus("idle");
    };
    // ------------------------------------------------------------------------
    // PTT and Live collapsed into a single mic mute/unmute toggle (owner
    // decision 2026-07-03): the tap-toggle PTT model degenerated into "toggle
    // listening", which is what Live already was — two controls with no real
    // difference. M now gates whether the mic may listen; turn segmentation
    // is the engine's job (native end-of-turn detection lives server-side).
    const toggleMic = () => {
      if (getModal()) {
        return;
      }
      // With the lane down, M is the retry control instead of a dead switch.
      const laneStatus = getLaneStatus();
      if (laneStatus === "offline" || laneStatus === "idle") {
        startVoiceLane();
        return;
      }
      mutate(() => {
        const next = !getMicLive();
        setMicLive(next);
        if (!next && laneStatus === "speaking") {
          // Muting mid-reply is an explicit interruption, not just a gate.
          laneClient.send({ ...protocolBase("barge_in"), reason: "user.mute" });
        }
        laneClient.send({ ...protocolBase("live.set"), value: next, reason: "user.toggle" });
        setUserText(next ? "Mic is live. Speak normally." : "Mic is muted. Tap M to talk.");
        appendTranscript("user", next ? "Mic unmuted." : "Mic muted.");
        recordSmoke("mic.state", { live: next, via: next ? "m-unmute" : "m-mute" });
      });
    };

    // One visible key per action (owner spec 2026-07-03): M is the only mic
    // control. Modal-scoped keys (c copy, j/k scroll, enter confirm, h from
    // the End Session dialog) are handled by the swallow guard while a modal
    // is open, so they never need mode bindings here.
    const modeBindings = [
      { key: "escape", cmd: "mortic.escape", desc: "End session / close modal" },
      { key: "m", cmd: "mortic.mic.toggle", desc: "Toggle mic" },
      { key: "x", cmd: "mortic.clear", desc: "Clear lane" },
      { key: "t", cmd: "mortic.transcript", desc: "Transcript" },
      { key: "h", cmd: "mortic.handoff", desc: "Handoff" }
    ];

    // Typing lock: global key handlers run before renderable handlers and the
    // prompt input skips defaultPrevented events, so swallowing unbound keys
    // here keeps focus-mode typing out of the OpenCode prompt. Keys bound by
    // the mortic.sidepod layer and any ctrl/meta chords pass through untouched.
    const morticModeKeys = new Set(modeBindings.map((binding) => binding.key.toLowerCase()));
    const swallowGuard = (event) => {
      if (!getFocused()) return;
      if (event?.ctrl || event?.meta || event?.super) return;
      const name = typeof event?.name === "string" ? event.name.toLowerCase() : "";
      const modal = getModal();
      if (modal) {
        // Modal-scoped keys. Esc passes through so the host dialog and the
        // mortic.escape binding can close the modal (closeModal is idempotent).
        if (name === "escape") return;
        event?.preventDefault?.();
        event?.stopPropagation?.();
        if (name === "c") {
          copyModal();
        } else if (modal === "exit" && name === "h") {
          openHandoff();
        } else if (modal === "exit" && (name === "enter" || name === "return")) {
          endSession();
        } else if (modal === "transcript" && (name === "j" || name === "down")) {
          scrollModal(1);
        } else if (modal === "transcript" && (name === "k" || name === "up")) {
          scrollModal(-1);
        } else {
          recordSmoke("typing.swallow", { key: name || "unknown", modal });
        }
        return;
      }
      if (morticModeKeys.has(name)) return;
      recordSmoke("typing.swallow", { key: name || "unknown" });
      event?.preventDefault?.();
      event?.stopPropagation?.();
    };
    const keyInput = api.renderer.keyInput;
    if (keyInput?.prependListener) {
      keyInput.prependListener("keypress", swallowGuard);
    } else {
      keyInput?.on?.("keypress", swallowGuard);
    }
    api.lifecycle.onDispose(() => {
      keyInput?.off?.("keypress", swallowGuard);
    });

    const actions = {
      toggleMic,
      clear: clearLane,
      openTranscript,
      openHandoff,
      requestEnd
    };

    // Palette layer must stay unpinned: mode-pinned layers are not "reachable"
    // from the prompt's slash menu, and the slash menu only lists commands
    // that carry a flat `slashName` (verified against OpenCode 1.17.13).
    api.keymap.registerLayer({
      commands: [
        {
          name: "mortic.focus",
          title: "Mortic: Focus sidepod",
          desc: "Focus the Mortic sidepod",
          category: "Mortic",
          namespace: "palette",
          slashName: "mortic",
          run: focusMortic
        }
      ],
      bindings: [{ key: "ctrl+x v", cmd: "mortic.focus", desc: "Focus Mortic sidepod" }]
    });

    api.keymap.registerLayer({
      mode: "mortic.sidepod",
      commands: [
        { name: "mortic.escape", title: "Mortic: End session / close modal", category: "Mortic", run: handleEscape },
        { name: "mortic.mic.toggle", title: "Mortic: Toggle mic", category: "Mortic", run: toggleMic },
        { name: "mortic.clear", title: "Mortic: Clear lane", category: "Mortic", run: clearLane },
        { name: "mortic.transcript", title: "Mortic: Transcript", category: "Mortic", run: openTranscript },
        { name: "mortic.handoff", title: "Mortic: Handoff", category: "Mortic", run: openHandoff }
      ],
      bindings: modeBindings
    });

    api.lifecycle.onDispose(() => {
      if (animationTimer) {
        clearInterval(animationTimer);
      }
    });
    api.lifecycle.onDispose(() => exitMorticMode?.());
    api.lifecycle.onDispose(() => {
      stopVoiceLane("client.shutdown");
      stopHelper();
    });

    // Memoized so sidebar_content and session_prompt_right (both re-rendered
    // by the host on every requestRender) share one computed state instead of
    // rebuilding it twice. The annex gets its own narrower memo so an
    // animation-only tick (phase) doesn't invalidate it — its output only
    // ever depends on focus/mic state.
    const getSidebarState = createMemo(() => {
      const transcript = getTranscript();
      return {
        micLive: getMicLive(),
        focused: getFocused(),
        laneStatus: getLaneStatus(),
        phase: getPhase(),
        userText: getUserText(),
        assistantText: getAssistantText(),
        transcript,
        handoffReady: transcript.length > 1
      };
    });
    const getAnnexState = createMemo(() => ({
      focused: getFocused(),
      micLive: getMicLive(),
      laneStatus: getLaneStatus()
    }));

    api.slots.register({
      order: 760,
      slots: {
        sidebar_content: () => renderPod(getSidebarState(), actions, api.theme.current),
        session_prompt_right: () => renderPromptAnnex(getAnnexState(), api.theme.current)
      }
    });
}

const plugin = { id, tui };

export default plugin;

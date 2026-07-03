import { appendFileSync } from "node:fs";
import { createElement, insert, setProp } from "@opentui/solid";
import { createSignal } from "solid-js";

const WIDTH = 36;
const INNER = WIDTH - 2;
// Centered host dialogs are wider than the sidepod column.
const MODAL_INNER = 60;
const MODAL_PAGE = 14;
const SMOKE_SINK = globalThis.process?.env?.MORTIC_SMOKE_LOG;
const HELPER_WS_URL = globalThis.process?.env?.MORTIC_HELPER_WS_URL ?? "ws://127.0.0.1:8765/ws/sidepod";

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

function row(left, right, width = INNER) {
  const gap = Math.max(1, width - left.length - right.length);
  return `${left}${" ".repeat(gap)}${right}`.slice(0, width);
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

function commandRow(key, label, state, color, onMouseDown) {
  return clickable(`║ ${fit(key, 6)} ${fit(label, 15)} ${fit(state, 9)} ║`, color, onMouseDown);
}

function renderHero(state, theme) {
  // TuiThemeCurrent declares these colors non-optional; no fallbacks needed.
  const accent = theme.accent;
  const muted = theme.textMuted;
  const secondaryAccent = theme.secondary;
  const ok = theme.success;
  const active = state.live || state.armed;
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
        center(active ? "VOICE SIGNAL OPEN" : "PRESS PTT OR LIVE", INNER)
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
  const armColor = state.armed ? theme.success : muted;
  const liveColor = state.live ? theme.success : muted;
  return [
    line("", muted),
    line(`╔ COMMAND DECK${"═".repeat(WIDTH - 15)}╗`, accent),
    commandRow("[M]", "Push to Talk", state.armed ? "ARMED" : "OFF", armColor, actions.ptt),
    commandRow("[L]", "Live", state.live ? "ON" : "OFF", liveColor, actions.toggleLive),
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

function transcriptText(state) {
  return state.transcript.map((item) => `${item.role}: ${item.text}`).join("\n");
}

function handoffText(state) {
  return [
    "Mortic handoff draft",
    "",
    `User intent: ${state.userText}`,
    `Assistant result: ${state.assistantText}`,
    "",
    "Keep code, commands, paths, diffs, and JSON screen-only."
  ].join("\n");
}

// PTT is a plain M toggle by product decision (2026-07-03). Terminal
// key-release reporting is too inconsistent to build on: iTerm2 and
// Terminal.app never send releases for plain keys, and the Kitty flag
// escalation needed for the rest added more complexity than the hold
// interaction was worth for v1.

// console output in a raw TUI is painted over by screen redraws and never
// reaches opencode.log, so smoke diagnostics only exist behind the durable
// opt-in sink: MORTIC_SMOKE_LOG=/tmp/mortic-smoke.log opencode
function logSmoke(api, event, details = {}) {
  if (!SMOKE_SINK) {
    return;
  }
  const payload = {
    event,
    mode: api.mode.current?.(),
    useKittyKeyboard: Boolean(api.renderer.useKittyKeyboard),
    at: new Date().toISOString(),
    ...details
  };
  console.info("[mortic smoke]", JSON.stringify(payload));
  try {
    appendFileSync(SMOKE_SINK, JSON.stringify(payload) + "\n");
  } catch {
    // the smoke sink must never break the TUI
  }
}

function createProtocolSender(recordSmoke) {
  const WebSocketCtor = globalThis.WebSocket;
  let socket;
  const queue = [];
  const flush = () => {
    while (socket?.readyState === 1 && queue.length) {
      socket.send(queue.shift());
    }
  };
  const open = () => {
    if (!WebSocketCtor) {
      recordSmoke("protocol.unavailable", { reason: "websocket-missing" });
      return;
    }
    if (socket && (socket.readyState === 0 || socket.readyState === 1)) {
      return;
    }
    try {
      socket = new WebSocketCtor(HELPER_WS_URL);
      socket.onopen = flush;
      socket.onclose = () => {
        socket = undefined;
      };
      socket.onerror = () => {
        recordSmoke("protocol.unavailable", { reason: "websocket-error" });
      };
    } catch {
      recordSmoke("protocol.unavailable", { reason: "websocket-open-failed" });
    }
  };
  return (payload) => {
    const serialized = JSON.stringify(payload);
    recordSmoke("protocol.send", { type: payload.type, turnId: payload.turnId });
    if (socket?.readyState === 1) {
      socket.send(serialized);
      return;
    }
    // Never replay a long backlog of stale PTT events when the helper
    // reconnects: keep only a short queue and drop the oldest beyond it.
    queue.push(serialized);
    if (queue.length > 16) {
      queue.shift();
      recordSmoke("protocol.drop", { reason: "queue-full" });
    }
    open();
  };
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
    const [getArmed, setArmed] = createSignal(false);
    const [getLive, setLive] = createSignal(false);
    const [getFocused, setFocused] = createSignal(false);
    const [getModal, setModal] = createSignal(null);
    const [getModalScroll, setModalScroll] = createSignal(0);
    const [getPhase, setPhase] = createSignal(0);
    const [getEvent, setEvent] = createSignal("ready");
    const [getUserText, setUserText] = createSignal("Press PTT or Live. Spoken asks will appear here.");
    const [getAssistantText, setAssistantText] = createSignal("Mortic replies with short speakable text. Details stay screen-only.");
    const [getTranscript, setTranscript] = createSignal([
      { role: "system", text: "Mortic sidepod loaded." },
      { role: "assistant", text: "Ready for push-to-talk." }
    ]);

    const requestRender = () => api.renderer.requestRender();
    // The orb animation ticks only while the voice lane is active; idle
    // sessions run no timer at all.
    let animationTimer;
    const syncAnimation = () => {
      const active = getArmed() || getLive();
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
    let turnSeq = 0;
    let activePttTurnId;
    let activePttStartEventId;

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
        setEvent("focus mode");
      });
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
          : ["No transcript yet.", "Use PTT or Live to start."];
        const top = Math.min(getModalScroll(), Math.max(0, all.length - MODAL_PAGE));
        return all.slice(top, top + MODAL_PAGE);
      }
      if (kind === "handoff") {
        return handoffText({ userText: getUserText(), assistantText: getAssistantText() })
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
          setEvent("confirm exit");
        } else {
          recordSmoke("modal.open", { modal: kind });
          setEvent(kind);
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
        return transcriptText({ transcript: getTranscript() });
      }
      if (getModal() === "handoff") {
        return handoffText({ userText: getUserText(), assistantText: getAssistantText() });
      }
      return "";
    };
    const copyModal = () =>
      mutate(() => {
        const value = modalCopyText();
        if (!value) {
          return;
        }
        recordSmoke("popup.copy", { popup: getModal() });
        // OSC 52 works in any terminal, including over SSH.
        const ok = api.renderer.copyToClipboardOSC52?.(value);
        api.ui.toast({ variant: ok ? "success" : "error", message: ok ? "Copied" : "Copy unavailable" });
        setEvent(ok ? "copied" : "copy unavailable");
      });
    const endSession = () =>
      mutate(() => {
        recordSmoke("exit.confirmed");
        setModal(null);
        api.ui.dialog.clear();
        setArmed(false);
        setLive(false);
        setEvent("session ended");
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
    const toggleLive = () => {
      if (getModal()) {
        return;
      }
      mutate(() => {
        const next = !getLive();
        setLive(next);
        setArmed(false);
        setEvent(next ? "live on" : "live off");
        setUserText(next ? "Live voice control is on." : "Live voice control is off.");
        appendTranscript("user", next ? "Live voice enabled." : "Live voice disabled.");
      });
    };
    const clearLane = () => {
      if (getModal()) {
        return;
      }
      mutate(() => {
        setArmed(false);
        setLive(false);
        setEvent("cleared");
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
    const sendProtocol = createProtocolSender(recordSmoke);
    const nextClientEventId = () => `evt_sidepod_${Date.now().toString(36)}_${++clientEventSeq}`;
    const nextTurnId = () => `turn_${Date.now().toString(36)}_${++turnSeq}`;
    const protocolBase = (type) => ({
      type,
      clientEventId: nextClientEventId(),
      sentAt: new Date().toISOString()
    });
    // Plain M toggle by product decision (2026-07-03): every M press flips
    // armed/stopped. No repeat debounce, no event-type handling, no timing.
    const handlePttKey = () => {
      if (getModal()) {
        return;
      }
      mutate(() => {
        if (getArmed()) {
          const stopEvent = protocolBase("ptt.stop");
          sendProtocol({
            ...stopEvent,
            matchingStartEventId: activePttStartEventId,
            turnId: activePttTurnId,
            reason: "tap.toggle",
            eventType: "tap"
          });
          activePttTurnId = undefined;
          activePttStartEventId = undefined;
          setArmed(false);
          setLive(false);
          setEvent("m stopped");
          setUserText("Push-to-talk stopped.");
          appendTranscript("user", "M PTT stopped.");
          recordSmoke("ptt.state", { armed: false, via: "m-stop" });
          return;
        }
        const startEvent = protocolBase("ptt.start");
        activePttTurnId = nextTurnId();
        activePttStartEventId = startEvent.clientEventId;
        sendProtocol({
          ...startEvent,
          turnId: activePttTurnId,
          inputMode: "ptt",
          key: "M",
          eventType: "press"
        });
        setArmed(true);
        setLive(false);
        setEvent("m armed");
        setUserText("Push-to-talk on. Tap M again to stop.");
        appendTranscript("user", "M PTT on.");
        recordSmoke("ptt.state", { armed: true, via: "m-arm" });
      });
    };

    // One visible key per action (owner spec 2026-07-03): M is the only PTT
    // key. Modal-scoped keys (c copy, j/k scroll, enter confirm, h from the
    // End Session dialog) are handled by the swallow guard while a modal is
    // open, so they never need mode bindings here.
    const modeBindings = [
      { key: "escape", cmd: "mortic.escape", desc: "End session / close modal" },
      { key: "m", cmd: "mortic.ptt.press", desc: "Push to talk" },
      { key: "l", cmd: "mortic.live", desc: "Toggle Live" },
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
      ptt: handlePttKey,
      toggleLive,
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
        { name: "mortic.ptt.press", title: "Mortic: Push-to-talk toggle", category: "Mortic", run: handlePttKey },
        { name: "mortic.live", title: "Mortic: Toggle Live", category: "Mortic", run: toggleLive },
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

    api.slots.register({
      order: 760,
      slots: {
        sidebar_content: () =>
          renderPod(
            {
              armed: getArmed(),
              live: getLive(),
              focused: getFocused(),
              phase: getPhase(),
              event: getEvent(),
              userText: getUserText(),
              assistantText: getAssistantText(),
              transcript: getTranscript(),
              handoffReady: getTranscript().length > 1
            },
            actions,
            api.theme.current
          )
      }
    });
}

const plugin = { id, tui };

export default plugin;

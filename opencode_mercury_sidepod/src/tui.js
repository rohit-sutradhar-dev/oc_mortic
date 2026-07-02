import { createElement, insert, setProp } from "@opentui/solid";
import { createSignal } from "solid-js";

const WIDTH = 36;
const INNER = WIDTH - 2;

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
    style === "modal"
      ? { tl: "┏", tr: "┓", bl: "┗", br: "┛", h: "━", v: "┃" }
      : style === "soft"
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

function separator(color, left = "╟", fill = "─", right = "╢") {
  return line(`${left}${fill.repeat(WIDTH - 2)}${right}`, color);
}

function statusGlyph(state) {
  if (state.live) {
    return "LIVE";
  }
  if (state.armed) {
    return "ARM";
  }
  return "IDLE";
}

function sphereSprite(phase, active, captionColor) {
  return renderBrailleOrb(phase, active, captionColor);
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
            bits |= brailleBit(localX, localY);
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

function brailleBit(x, y) {
  const map = [
    [0x01, 0x08],
    [0x02, 0x10],
    [0x04, 0x20],
    [0x40, 0x80]
  ];
  return map[y][x];
}

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
  const accent = theme.accent ?? theme.primary ?? theme.text;
  const muted = theme.textMuted ?? theme.text;
  const secondaryAccent = theme.secondaryAccent ?? theme.accentSecondary ?? theme.secondary ?? theme.warning ?? theme.info ?? accent;
  const ok = theme.success ?? accent;
  const active = state.live || state.armed;
  const color = active ? ok : muted;
  const border = state.focused ? secondaryAccent : accent;
  return [
    ...frame(
      "MORTIC",
      [
        center("M O R T I C", INNER),
        row("focus", state.focused ? "sidepod" : "prompt"),
        row("voice lane", statusGlyph(state)),
        center("", INNER),
        ...sphereSprite(state.phase, active, secondaryAccent).map((item) =>
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
  const accent = theme.accent ?? theme.primary ?? theme.text;
  const muted = theme.textMuted ?? theme.text;
  const ok = theme.success ?? accent;
  const warn = theme.warning ?? accent;
  const armColor = state.armed ? ok : muted;
  const liveColor = state.live ? ok : muted;
  return [
    line("", muted),
    line(`╔ COMMAND DECK${"═".repeat(WIDTH - 16)}╗`, accent),
    line(`║${fit(row("last", state.event), INNER)}║`, muted),
    line(`║${fit(row("items", String(state.transcript.length)), INNER)}║`, muted),
    separator(accent),
    commandRow("[PTT]", "Push to Talk", state.armed ? "ARMED" : "OFF", armColor, actions.toggleArmed),
    commandRow("[LIVE]", "Voice Control", state.live ? "ON" : "OFF", liveColor, actions.toggleLive),
    commandRow("[CLR]", "Clear Lane", "RESET", warn, actions.clear),
    commandRow("[TRN]", "Transcript", "POPUP/C", accent, actions.openTranscript),
    commandRow("[HND]", "Handoff", state.handoffReady ? "READY" : "DRAFT", accent, actions.openHandoff),
    line(`╚${"═".repeat(WIDTH - 2)}╝`, accent)
  ];
}

function renderConversation(state, theme) {
  const accent = theme.accent ?? theme.primary ?? theme.text;
  const muted = theme.textMuted ?? theme.text;
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

function renderPopup(state, actions, theme) {
  if (!state.popup) {
    return [];
  }
  const accent = theme.accent ?? theme.primary ?? theme.text;
  const danger = theme.error ?? accent;
  const isTranscript = state.popup === "transcript";
  const title = isTranscript ? "TRANSCRIPT" : "HANDOFF";
  const lines = isTranscript ? transcriptLines(state) : handoffLines(state);
  return [
    line("", theme.textMuted ?? theme.text),
    ...frame(title, lines.slice(0, 9), theme.text, accent, "modal"),
    clickable(`┃  C COPY${" ".repeat(Math.max(0, INNER - 8))}┃`, accent, () => actions.copy(isTranscript ? transcriptText(state) : handoffText(state))),
    clickable(`┃  X CLOSE${" ".repeat(Math.max(0, INNER - 9))}┃`, danger, actions.closePopup),
    line(`┗${"━".repeat(WIDTH - 2)}┛`, accent)
  ];
}

function transcriptText(state) {
  return state.transcript.map((item) => `${item.role}: ${item.text}`).join("\n");
}

function transcriptLines(state) {
  if (!state.transcript.length) {
    return ["No transcript yet.", "Use PTT or Live to start."];
  }
  return state.transcript.flatMap((item) => wrap(`${item.role}: ${item.text}`, INNER));
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

function handoffLines(state) {
  return wrap("Handoff draft prepared from the current voice lane. Spoken text stays short; screen-only details stay separate.", INNER);
}

function copyToClipboard(value) {
  try {
    if (globalThis.Bun?.spawnSync) {
      globalThis.Bun.spawnSync(["pbcopy"], { stdin: value });
      return true;
    }
  } catch {
    return false;
  }
  return false;
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
      ...renderConversation(state, theme),
      ...renderPopup(state, actions, theme)
    ]
  );
}

const plugin = {
  id: "mortic-sidepod:tui",
  tui: async (api) => {
    const [getArmed, setArmed] = createSignal(false);
    const [getLive, setLive] = createSignal(false);
    const [getFocused, setFocused] = createSignal(false);
    const [getPopup, setPopup] = createSignal(null);
    const [getPhase, setPhase] = createSignal(0);
    const [getEvent, setEvent] = createSignal("ready");
    const [getUserText, setUserText] = createSignal("Press PTT or Live. Spoken asks will appear here.");
    const [getAssistantText, setAssistantText] = createSignal("Mortic replies with short speakable text. Details stay screen-only.");
    const [getTranscript, setTranscript] = createSignal([
      { role: "system", text: "Mortic sidepod loaded." },
      { role: "assistant", text: "Ready for push-to-talk." }
    ]);

    const requestRender = () => api.renderer.requestRender();
    const mutate = (fn) => {
      fn();
      requestRender();
    };
    const appendTranscript = (role, value) => {
      setTranscript([...getTranscript(), { role, text: value }].slice(-12));
    };
    let exitMorticMode;

    const focusMortic = () =>
      mutate(() => {
        if (!exitMorticMode) {
          exitMorticMode = api.mode.push("mortic.sidepod");
        }
        setFocused(true);
        setEvent("focus mode");
      });
    const blurMortic = () =>
      mutate(() => {
        if (exitMorticMode) {
          exitMorticMode();
          exitMorticMode = undefined;
        }
        setFocused(false);
        setEvent("prompt mode");
      });
    const toggleArmed = () =>
      mutate(() => {
        const next = !getArmed();
        setArmed(next);
        setLive(false);
        setEvent(next ? "ptt armed" : "ptt muted");
        setUserText(next ? "Listening while push-to-talk is held." : "Push-to-talk released.");
        appendTranscript("user", next ? "Push-to-talk armed." : "Push-to-talk released.");
      });
    const toggleLive = () =>
      mutate(() => {
        const next = !getLive();
        setLive(next);
        setArmed(false);
        setEvent(next ? "live on" : "live off");
        setUserText(next ? "Live voice control is on." : "Live voice control is off.");
        appendTranscript("user", next ? "Live voice enabled." : "Live voice disabled.");
      });
    const clearLane = () =>
      mutate(() => {
        setPopup(null);
        setArmed(false);
        setLive(false);
        setEvent("cleared");
        setUserText("Voice lane cleared.");
        setAssistantText("Ready for the next spoken turn.");
        setTranscript([{ role: "system", text: "Voice lane cleared." }]);
      });
    const openTranscript = () =>
      mutate(() => {
        setPopup(getPopup() === "transcript" ? null : "transcript");
        setEvent("transcript");
      });
    const openHandoff = () =>
      mutate(() => {
        setPopup(getPopup() === "handoff" ? null : "handoff");
        setEvent("handoff");
      });
    const closePopup = () =>
      mutate(() => {
        setPopup(null);
        setEvent("closed");
      });
    const copyValue = (value) =>
      mutate(() => {
        setEvent(copyToClipboard(value) ? "copied" : "copy unavailable");
      });
    const actions = {
      toggleArmed,
      toggleLive,
      clear: clearLane,
      openTranscript,
      openHandoff,
      closePopup,
      copy: copyValue
    };

    api.keymap.registerLayer({
      mode: "base",
      commands: [
        {
          name: "mortic.focus",
          title: "Mortic: Focus sidepod",
          category: "Mortic",
          namespace: "palette",
          run: focusMortic
        }
      ],
      bindings: [{ key: "ctrl+x v", cmd: "mortic.focus", desc: "Focus Mortic sidepod" }]
    });

    api.keymap.registerLayer({
      mode: "mortic.sidepod",
      commands: [
        { name: "mortic.blur", title: "Mortic: Return to prompt", category: "Mortic", run: blurMortic },
        { name: "mortic.ptt", title: "Mortic: Push to Talk", category: "Mortic", run: toggleArmed },
        { name: "mortic.live", title: "Mortic: Toggle Live", category: "Mortic", run: toggleLive },
        { name: "mortic.clear", title: "Mortic: Clear lane", category: "Mortic", run: clearLane },
        { name: "mortic.transcript", title: "Mortic: Transcript popup", category: "Mortic", run: openTranscript },
        { name: "mortic.handoff", title: "Mortic: Handoff popup", category: "Mortic", run: openHandoff }
      ],
      bindings: [
        { key: "escape", cmd: "mortic.blur", desc: "Return to prompt" },
        { key: "p", cmd: "mortic.ptt", desc: "Push to Talk" },
        { key: "l", cmd: "mortic.live", desc: "Toggle Live" },
        { key: "c", cmd: "mortic.clear", desc: "Clear lane" },
        { key: "t", cmd: "mortic.transcript", desc: "Transcript popup" },
        { key: "h", cmd: "mortic.handoff", desc: "Handoff popup" }
      ]
    });

    const timer = setInterval(() => {
      if (getArmed() || getLive()) {
        setPhase((getPhase() + 1) % 8);
        requestRender();
      }
    }, 135);

    api.lifecycle.onDispose(() => clearInterval(timer));
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
              popup: getPopup(),
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
};

export default plugin;

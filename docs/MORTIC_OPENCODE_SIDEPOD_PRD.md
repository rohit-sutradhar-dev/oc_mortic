# Mortic OpenCode Native Sidepod PRD

Status: Draft for annotation
Audience: Technical product managers, engineering leads, design reviewers
Date: 2026-06-30
Primary surface: Native OpenCode TUI sidebar plugin
Backend direction: For now - Mercury Inception Key to be configured for local dev-server. Later we will build proxy to serve through our key without user visibility on what model we are running to get this done.

## Revision: Interaction Model (2026-07-03, owner-directed after live testing)

The shipped v1 interaction model supersedes the PTT/Live design throughout this document. Where text below conflicts with this section, this section wins; mockups and state machines below are kept for design-language reference only.

1. **PTT and Live are merged into a single mic mute/unmute toggle.** Hold-to-talk proved terminal-infeasible (no key-release events for plain keys in real terminals — full investigation in `docs/MORTIC_TERMINAL_CAPABILITY_SMOKE.md`), and the tap-toggle fallback had degenerated into "toggle listening", which is what Live already was. One state bit remains: mic muted or mic live. `M` toggles it. There is no separate `L Live` control. Turn segmentation (where a spoken turn starts and ends) belongs to the engine's end-of-turn detection, not to a UI key.
2. **Protocol**: the sidepod sends `live.set { value }` on every `M` press. `ptt.start`/`ptt.stop` remain defined in protocol v0 for a possible future hold-to-talk client but are not sent by the v1 sidepod.
3. **Command deck (shipped)**: `[M] Microphone` (LIVE/MUTED), `[X] Clear Lane`, `[T] Transcript`, `[H] Handoff`, `[ESC] End Session`. `C Config` is deferred (MOR-100); confirmed Refresh (`R`, engine fork reset) lands with the engine integration (MOR-96).
4. **Esc is never destructive.** Esc opens a centered End Session dialog (Mortic sessions are ephemeral per thread/fork to avoid stale-context sync); ending requires an explicit `enter` confirm inside that dialog, with `h` offered to open Handoff first. Esc inside any dialog closes/cancels only.
5. **Popups are centered host dialogs** (OpenCode's own dialog surface), not in-sidepod panels. Focus/typing-lock state renders persistently beside the OpenCode prompt row while Mortic is focused.
6. `/mortic` refuses to focus when no OpenCode session is open (the sidepod cannot render there) and explains why via toast, rather than silently locking typing.

## Revision: Enforced Contract And Live Lane (2026-07-04, owner + CTO)

1. **The protocol is enforced programmatically.** `protocol/schema.ts` (TypeScript) is the normative contract source; generated JSON Schema artifacts are validated at both WebSocket boundaries. The engine's sidepod lane fails closed — off-vocabulary messages are dropped and logged, never sent. See the amended `docs/MORTIC_PROTOCOL_V0.md`.
2. **Protocol v0 amendment**: explicit `stop` command + `stopped` ack event for lane teardown, and optional `start.opencodeUrl` pinning the engine to the OpenCode server that owns the focused thread. WebSocket disconnect without `stop` still tears down fully.
3. **The voice lane is live end to end**: `/mortic` discovers or launches the helper (non-blocking; the pod shows `CONNECTING VOICE…` / `VOICE OFFLINE · M TO RETRY`), `start` forks the focused thread, `M` drives real native capture through `live.set`, engine events (`transcript`, `thinking`, `assistant.delta`, `speaking`, `complete` with real latency, `voice_bridge_issue`, `stopped`) render through a sequence/stale-turn-guarded reducer, and End Session performs acknowledged teardown with fork deletion.
4. **Mute mid-reply is a privacy-only soft mute.** Capture stops immediately while the current reply continues on the persistent playback/AEC clock; `X`, spoken `stop`/`wait`, or committed barge-in cancels the reply. A silent mic (permission denied) is detected by a capture watchdog and surfaces as `Mic permission needed`.

## 1. Executive Summary

Mortic should become a native OpenCode sidepod that lets a user speak to an isolated ephemeral fork of the current OpenCode thread with minimal friction. The user should be able to push-to-talk, leave live voice enabled, refresh/reset the voice fork, inspect the current spoken turn, open a transcript popup, and generate/copy a handoff without leaving the keyboard.

The old Electron app provides the feature baseline: a fast, crisp dark voice console with push-to-talk, live mode, transcript, handoff, status messages, and TTS playback. Its typed fallback and visible browser UI are not part of the packaged native product. The native OpenCode plugin changes the product shape: because the sidepod is already attached to the active OpenCode thread, it does not need thread selection. It should instead operate directly on the current thread, create an ephemeral fork for voice interaction, and delete that fork by default when the interaction ends.

The first native version should preserve the existing sidepod's visual language: compact terminal geometry, cyan accent, boxed sections, crisp typography, keyboard commands, and a pulsing braille/block voice sprite. The product should remove runtime/model/provider reporting from the main UI. Mercury/Inception should power the backend, using the local `.env` Inception key for now, but the user-facing product should simply feel like Mortic speaking with the current thread.

## 2. Product Thesis

OpenCode is already where the developer thinks, edits, and supervises agent work. Mortic should not be a separate app that asks the user to manage another session picker. It should be a voice lane embedded inside the OpenCode thread, optimized for fast spoken interaction:

- the user speaks naturally;
- Mortic responds quickly with speakable prose;
- code, diffs, commands, paths, and JSON remain screen-only;
- the submitted turn and truthful work activity are visible immediately, followed by one atomic validated answer;
- the user can interrupt, refresh/reset, or hand off without touching the mouse.

## 3. Goals

1. Make voice interaction feel native to OpenCode.
2. Preserve the old Electron feature base that matters for voice workflow: PTT, Live, Refresh, COMMS, Transcript, Handoff, status, and TTS.
3. Remove thread selection from the product surface because the active OpenCode thread is already known.
4. Keep runtime/provider/model names out of the primary UI.
5. Keep the existing fast Mercury backend path, using `.env` for the Inception key in the first implementation.
6. Keep Deepgram Flux STT and Deepgram Speak/Aura TTS behind an invisible local bridge/native helper.
7. Maintain ephemeral fork safety: source OpenCode thread remains untouched unless the user explicitly chooses to copy/apply handoff text.
8. Support mouse interaction, but design for keyboard-first use.
9. Make the current turn legible while it is happening: show recognition and generic work activity immediately, then replace it atomically with the validated assistant response.
10. Provide a high-quality PRD artifact that can be annotated before implementation begins.

## 4. Non-Goals

1. No implementation in this PRD phase.
2. No BYOK UI in v1. The first version uses local `.env` keys.
3. No hosted proxy service in v1.
4. No separate Electron overlay in v1.
5. No OpenCode thread picker in the sidepod.
6. No provider/model/runtime reporting in normal UI.
7. No typed fallback in the packaged sidepod. If the user wants to type, they can use the normal OpenCode prompt beside the sidepod.
8. No visible browser UI in the packaged main path. The browser implementation is a technical/performance reference only.
9. No attempt to make the TUI itself directly own OS microphone capture; the invisible local bridge/native helper owns capture and playback.
10. No full historical OpenCode thread viewer inside the sidepod.

## 5. Source Inputs

### 5.1 Old Electron App Observations

The screenshots show a dark, cyan-accented Mortic app with:

- brand header: `Mortic` with a cyan status dot;
- voice-oriented workspace label;
- scratch/current conversation area;
- large PTT button, shown as `Hold M`;
- Stop/Audio control;
- Handoff section with generate/copy controls;
- config/status strip;
- debug trace/status messages;
- current spoken/assistant turn shown in a large card;
- transcript affordance;
- compact, developer-oriented type and spacing.

These features should inform the native plugin, but the layout must be adapted to an OpenCode sidebar rather than a standalone app. The old typed prompt and visible browser shell should not carry into the packaged product.

### 5.2 Existing Native Sidepod Observations

The current native sidepod proof already provides the right design direction:

- right sidebar panel;
- `MORTIC` hero section;
- animated/pulsing sphere;
- command deck;
- COMMS panel;
- Transcript popup;
- Handoff popup;
- keyboard focus mode;
- OpenCode command-palette entries;
- keybindings for PTT, Live, Refresh, Transcript, Handoff, and Escape.

The sidepod currently has placeholder labels `last` and `items` in the command deck. In the product spec, these should be removed or renamed because they are implementation diagnostics, not user-facing concepts.

## 6. Product Surface

### 6.1 Primary Surface

Mortic lives in the OpenCode sidebar as a native TUI plugin panel.

The panel should be attached to the current OpenCode thread. It should infer the active session from OpenCode/plugin context and should not ask the user to choose a thread.

### 6.2 Backend/Helper Surface

The OpenCode TUI sidepod is the only packaged keyboard/control/rendering surface. It owns:

- PTT controls;
- Live controls;
- Refresh/reset command;
- Config stub;
- COMMS rendering;
- Transcript popup;
- Handoff popup.

An invisible local bridge/native helper owns:

- OS microphone capture;
- Deepgram Flux STT;
- Deepgram TTS playback;
- Mercury/OpenCode ephemeral fork turns;
- barge-in;
- speech filtering;
- event-stream and polling fallback behavior.

Recorded browser-era benchmarks remain historical comparison data. The packaged native helper and sidepod are now the only product path and must preserve or improve the measured latency and speech behavior.

## 7. Information Architecture

The sidepod should contain four primary zones:

1. Voice Status Hero
2. COMMS Current Turn
3. Command Deck
4. Popup Layer

An optional small internal status line may appear only when needed for actionable state such as refreshing, missing mic permission, or helper unavailable. It should not show model/runtime/provider names.

### 7.1 Desired Default Layout

```text
╔ MORTIC ◉ ════════════════════════╗
║                                  ║
║             ⢀⣴⣿⣿⣿⣿⣦⡀            ║
║             ⣿⣿⣿ready⣿⣿⣿            ║
║             ⣿⣿⣿⣿⣿⣿⣿⣿            ║
║             ⠈⠻⣿⣿⣿⣿⠟⠁            ║
║                                  ║
╚══════════════════════════════════╝

╔ COMMS ══════════════════════════════╗
║ YOU                                 ║
║ Currently in Mortic.                ║
║                                     ║
║ MORTIC                              ║
║ Ready for a spoken turn.            ║
╚═════════════════════════════════════╝

╔ COMMAND DECK ═══════════════════════╗
║ M Hold PTT        L Live            ║
║ R Refresh         C Config          ║
║ T Transcript      H Handoff         ║
╚═════════════════════════════════════╝
```

### 7.2 Compact Sidebar Layout

When width is constrained, commands should remain legible and stable:

```text
╔ MORTIC ◉ ══════════╗
║      ⣿ready⣿      ║
╚═════════════════════╝
╔ COMMS ══════════════╗
║ YOU                 ║
║ Fix the failing...  ║
║ MORTIC              ║
║ I’ll update the...  ║
╚═════════════════════╝
╔ COMMANDS ═══════════╗
║ M PTT    L Live     ║
║ R Ref    C Config   ║
║ T Tran   H Hand     ║
╚═════════════════════╝
```

### 7.3 Wide Sidebar Layout

When more width is available, the sidepod may use more descriptive labels but should keep the same section order:

```text
╔ MORTIC ◉ ═════════════════════════════════════╗
║                                              ║
║                ⢀⣴⣿⣿⣿⣿⣦⡀              ║
║                ⣿⣿listening⣿⣿              ║
║                ⣿⣿⣿⣿⣿⣿⣿⣿              ║
║                ⠈⠻⣿⣿⣿⣿⠟⠁              ║
║                                              ║
╚═════════════════════════════════════════════════╝

╔ COMMS - CURRENT TURN ══════════════════════════╗
║ YOU                                            ║
║ Can you make the test output easier to scan?   ║
║                                                ║
║ MORTIC                                         ║
║ Yes. I’ll tighten the failure summary and      ║
║ write the code changes into the files directly.║
╚═════════════════════════════════════════════════╝

╔ COMMAND DECK ══════════════════════════════════╗
║ M Hold PTT       L Live Voice      R Refresh   ║
║ C Config         T Transcript      H Handoff   ║
╚═════════════════════════════════════════════════╝
```

## 8. Visual Design Requirements

### 8.1 Design Language

The sidepod should mostly preserve the existing OpenCode extension proof:

- terminal-native boxes and borders;
- `MORTIC ◉` in the top-left border of the hero;
- current extension color scheme retained as-is;
- no new palette, accent remapping, or color reinterpretation in v1;
- current dark background, text colors, accent colors, and sprite colors preserved;
- tight spacing;
- no marketing copy;
- no large explanatory paragraphs;
- no card-within-card nesting;
- no runtime/model badges in the normal UI.

### 8.2 Motion

The animated sprite should communicate state. It should use the same pulsing language as the current extension: a compact braille/block globe that brightens, dims, and subtly changes texture over time. The less the sprite design changes from the current extension, the better.

State text must go through the sprite itself, matching the current `thinking` overlay style. Do not add separate hero status text underneath the sprite.

- Idle/ready: current idle pulse with centered `ready` text in the sprite.
- Listening/PTT: current active pulse with centered `listening` text in the sprite.
- Live: current active/open pulse with centered `listening` or `live` text in the sprite.
- Thinking: current subtle pulse with centered `thinking` text in the sprite.
- Speaking: current active pulse with centered `speaking` text in the sprite.
- Error: current muted/warning style with concise issue text in the sprite if it fits.

The hero state vocabulary is:

- `ready`
- `listening`
- `thinking`
- `speaking`
- `interrupted`
- `voice issue`

### 8.3 Typography

The UI should use the terminal font provided by OpenCode/TUI. Labels should be short and scan-friendly.

Recommended labels:

- `MORTIC`
- `COMMS`
- `COMMAND DECK`
- `TRANSCRIPT`
- `HANDOFF`
- `Ready`
- `Listening`
- `Thinking`
- `Speaking`
- `Interrupted`
- `Refresh`
- `Config`

Avoid:

- provider names;
- runtime names;
- verbose status narration;
- full stack traces;
- raw API errors unless in debug logs.

## 9. Command Deck

### 9.1 Required Commands

The command deck should expose exactly the controls a voice user expects:

> Revised 2026-07-03 (see the Revision section at the top): the shipped deck is `[M] Microphone`, `[X] Clear Lane`, `[T] Transcript`, `[H] Handoff`, `[ESC] End Session`. The Microphone row replaces both PTT and Live below.

| Command | Primary Key | Mouse Label | Behavior |
| --- | --- | --- | --- |
| Microphone (shipped) | `M` | `Microphone` | Toggles the mic live/muted; sends `live.set`. Turn boundaries come from engine end-of-turn detection. |
| ~~Push to Talk~~ (superseded) | `M` hold preferred | `Hold M` / `PTT` | Starts capture while active; sends final transcript on release/end-of-turn. |
| ~~Live Voice~~ (superseded) | `L` | `Live` | Toggles continuous listening with barge-in behavior. |
| Refresh | `R` | `Refresh` | Opens a confirmation prompt. On confirm, stops any active voice turn, discards the current ephemeral voice fork, creates a fresh fork from the active OpenCode thread, and resets COMMS to Ready. |
| Config | `C` | `C Config` | Non-functional stub for future account/API key/subscription settings. Included now to keep the command deck visually balanced and to reserve the product surface. |
| Transcript | `T` | `Transcript` | Opens transcript popup. |
| Handoff | `H` | `Handoff` | Opens handoff popup. |
| Close/Reset Focus | `Esc` | `Close` | Closes popup if one is open. Otherwise opens an exit confirmation prompt. On confirm, exits Mortic focus mode, stops active voice capture/playback best-effort, and returns focus to the OpenCode prompt. |

### 9.2 `last` and `items` Resolution

The current proof renders:

```text
last   <last local UI event>
items  <transcript entry count>
```

These should not ship as-is. They are developer diagnostics, not user-facing product concepts.

Recommended replacement:

- Remove both rows from the normal command deck.
- Surface state in the Hero and COMMS sections instead.
- If a compact status row is needed, use clear labels:
  - `NOW Listening`
  - `TURN 12`
  - `SYNC Ready`

Preferred v1:

```text
╔ COMMAND DECK ═══════════════════════╗
║ M Hold PTT        L Live            ║
║ R Refresh         C Config          ║
║ T Transcript      H Handoff         ║
╚═════════════════════════════════════╝
```

No `last`. No `items`.

### 9.3 Keyboard-First Interaction

The user should not need the mouse for normal operation.

Base mode:

- `/mortic`: focus Mortic sidepod without sending a conversation turn to the model.

Mortic focus mode (revised 2026-07-03; shipped set):

- `m`: toggle mic live/muted (replaces push-to-talk and Live).
- `x`: clear the voice lane.
- `t`: open Transcript dialog.
- `h`: open Handoff dialog.
- `escape`: close dialog if open; otherwise open the End Session dialog (ending requires explicit `enter` confirm).
- `r` (confirmed Refresh) and `c` (Config stub) are deferred to the engine integration and MOR-100 respectively.

The `/mortic` slash command is the preferred entrypoint because it creates a clear interaction boundary. While Mortic focus mode is active, ordinary typing should not advance the main OpenCode conversation. This prevents conflicts where the user is speaking to Mortic and accidentally sending a parallel typed instruction to the thread.

Config stub behavior:

- visible in the command deck;
- not functional in v1;
- `c` opens or focuses the stub in Mortic focus mode;
- may open a placeholder popup that says `Config coming later`, or may render disabled;
- reserved for future API key, subscription, account, and routing settings.

Popup mode:

- `j` / Down: scroll down.
- `k` / Up: scroll up.
- `g`: top.
- `G`: bottom.
- `c`: copy popup contents.
- `escape` / `x`: close popup.

Key conflict rule: `c` means Config only in Mortic focus mode. Inside Transcript and Handoff popups, `c` means copy popup contents.

M key isolation rule: after `/mortic` focuses the sidebar, `M` must be captured by Mortic's focus layer while pressed. `M` keydown, key repeat, and keyup should not leak into the OpenCode prompt or other OpenCode keymaps. This isolation does not need a separate visual mode; it should be made clear in the implementation contract and tested. If OpenCode TUI keymaps cannot detect key release, `M` should degrade from hold-to-talk to tap-to-arm/tap-to-release while preserving the `Hold M` visual only where true hold behavior is supported.

## 10. COMMS Current Turn

### 10.1 Purpose

COMMS is not the full transcript. It is the current spoken interaction lane. It should show what the user most needs while speaking:

- current user utterance as it is recognized;
- transient, generic work activity while the answer is pending;
- the final validated display response, atomically admitted separately from the natural spoken response;
- enough context to know whether Mortic heard correctly and is responding.

### 10.2 Behavior

COMMS should rerender as recognition or work activity changes. The final answer replaces progress atomically. If the current turn cannot fit in the box, it should auto-scroll to keep the most recent relevant text visible.

States:

1. Idle
   - `YOU`: empty or last submitted turn.
   - `MORTIC`: `Ready for a spoken turn.`

2. Listening
   - `YOU`: interim/final transcript grows as Deepgram emits text.
   - `MORTIC`: `Listening...`

3. Thinking
   - `YOU`: final submitted transcript.
   - `MORTIC`: starts with `Thinking...`, then may show one generic activity sentence such as `I’m reviewing the relevant files.`
   - tool names, arguments, paths, providers, percentages, model narration, and partial structured output are never shown.

4. Speaking
   - `YOU`: final submitted transcript.
   - `MORTIC`: the validated `displayText` appears once before its corresponding `spokenText` is sent to TTS.
   - screen and speech retain the same facts, certainty, qualifications, and ordering.

5. Interrupted
   - `YOU`: new speech starts.
   - `MORTIC`: previous speech stops immediately and lane switches to new turn.

6. Error
   - Show a concise actionable message, such as:
     - `Mic unavailable. Refresh or check permission.`
     - `Voice bridge disconnected. Press R to refresh.`
     - `This turn stopped before completion. Resend if needed.`

### 10.3 Screen-Only Content

The assistant may produce code, commands, paths, diffs, or JSON in the OpenCode thread. Mortic must not read those aloud.

COMMS should handle technical content through a natural validated summary:

```text
MORTIC
I wrote the implementation into the files. Details are in the thread.
```

It should never admit code blocks, raw commands, paths, JSON, tool payloads, or partially formed structured output into the spoken lane. The full OpenCode thread remains the source of truth for code changes and detailed output.

## 11. Transcript Popup

### 11.1 Purpose

Transcript lets the user review what was spoken during the Mortic sidepod session without scrolling the main OpenCode thread.

### 11.2 Content

The transcript popup should include:

- turn number;
- role label: `YOU` or `MORTIC`;
- final user transcript;
- assistant spoken response;
- interruption markers where applicable;
- concise error markers where applicable;
- timestamps or relative times only if they fit without clutter.

It should not include raw provider JSON or verbose logs.

### 11.3 Mockup

```text
┏ TRANSCRIPT ━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Turn 1                              ┃
┃ YOU                                 ┃
┃ Make the test summary easier to     ┃
┃ scan.                               ┃
┃                                     ┃
┃ MORTIC                              ┃
┃ I tightened the failure summary and ┃
┃ wrote the changes into the files.   ┃
┃                                     ┃
┃ Turn 2                              ┃
┃ YOU                                 ┃
┃ Also keep it compact.               ┃
┃                                     ┃
┃ j/k scroll  C copy  Esc close       ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

### 11.4 Copy Behavior

Pressing `C` in the transcript popup copies the transcript text to the clipboard.

Copy format:

```text
Mortic transcript

Turn 1
YOU: ...
MORTIC: ...

Turn 2
YOU: ...
MORTIC: ...
```

## 12. Handoff Popup

### 12.1 Purpose

Handoff turns the voice lane into a concise written summary that can be copied back into OpenCode or another planning surface.

It should summarize what was spoken about, not dump the entire transcript. It should be optimized for carrying the thread forward.

### 12.2 Handoff Actions

Required:

- `Generate`: produce or refresh the handoff summary.
- `Preview`: view generated handoff in the popup.
- `C Copy`: copy handoff contents.
- `Esc Close`: close popup.

Optional later:

- `Copy short`
- `Copy full`
- `Insert into current prompt`

### 12.3 Mockup

```text
┏ HANDOFF ━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Carry the thread forward            ┃
┃                                     ┃
┃ Summary                             ┃
┃ We discussed replacing the Electron ┃
┃ app with an OpenCode-native sidepod.┃
┃ The sidepod should keep PTT, Live,  ┃
┃ Refresh, Transcript, and Handoff.   ┃
┃ It should not show runtime names.   ┃
┃                                     ┃
┃ Next desired work                   ┃
┃ Wire the sidepod to the existing    ┃
┃ voice bridge after PRD approval.    ┃
┃                                     ┃
┃ G generate  C copy  Esc close       ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

### 12.4 Generated Handoff Format

```text
Mortic handoff

Context:
- The user wants ...

Decisions:
- ...

Open questions:
- ...

Next action:
- ...
```

The handoff should avoid mentioning hidden runtime/provider details unless the user explicitly discussed them as product requirements.

## 13. Refresh Behavior

Refresh is a first-class control in the native sidepod.

Refresh means "start the Mortic voice lane over from the current OpenCode thread." It is not a generic resync button and there is no separate Clear command in v1.

Pressing `R` should:

1. ask the user to confirm that they want to exit/reset the current voice lane;
2. cancel and return to the previous Mortic state if the user declines;
3. verify the local bridge is reachable if the user confirms;
4. stop any active STT/TTS playback;
5. abort any active OpenCode voice turn best-effort;
6. delete/discard the current ephemeral voice fork if one exists;
7. clear COMMS, local transcript, and handoff draft for the current voice lane;
8. create a fresh ephemeral fork from the active OpenCode source thread;
9. return the Hero/COMMS state to `Ready`;
10. leave the source OpenCode thread untouched.

User-facing statuses:

- `Refreshing`
- `Ready`
- `Voice Bridge Issue`
- `Mic permission needed`

Do not show:

- raw model IDs;
- provider keys;
- internal URLs unless in debug mode.

Design decision: Refresh absorbs the old "Clear" concept. It clears the current Mortic voice lane because it resets the ephemeral voice fork. No separate Clear button should appear in the packaged command deck.

## 14. Voice Interaction Model

> Revised 2026-07-03: 14.1 and 14.2 are merged in the shipped product — one mic toggle (`M`), continuous listening while live, engine end-of-turn detection segments turns, barge-in per 14.3. The subsections below record the original design.

### 14.1 Push-to-Talk (superseded)

Preferred user experience:

1. User types `/mortic` to focus Mortic without sending a conversation turn.
2. User holds `M`.
3. Hero switches to `Listening`.
4. COMMS shows interim transcript.
5. User releases `M`.
6. Final transcript is sent to the current thread's voice fork.
7. Thinking and observed work activity render in COMMS without exposing tool details or model narration.
8. The final validated display response appears atomically and its paired spoken response begins TTS.
9. Turn completes; transcript is persisted locally for popup/handoff.

### 14.2 Live Mode (superseded — now the only mode, gated by `M`)

Live mode is for longer conversational flow:

1. User presses `L`.
2. Hero switches to `Live`.
3. Deepgram Flux controls end-of-turn detection.
4. User can speak without holding `M`.
5. User can barge in by speaking while TTS is playing.
6. Pressing `L` again turns Live off.

Live mode should never feel like a hidden recorder. The Hero must clearly indicate when the voice lane is open.

### 14.3 Barge-In

When user speech starts during assistant speech:

- stop TTS playback immediately;
- abort active OpenCode turn best-effort;
- close or flush TTS stream;
- mark previous turn as interrupted in transcript;
- begin listening to new user speech;
- do not require mouse interaction.

### 14.4 No Typed Fallback

The packaged sidepod should not include a typed fallback. If the user wants to type, the normal OpenCode prompt is already adjacent to the sidepod and is the correct typing surface.

The sidepod remains focused on spoken interaction: PTT, Live, Refresh, COMMS, Transcript, and Handoff.

## 15. Backend Requirements

### 15.1 Existing Bridge Reuse

Use the existing Python helper behavior as the backend foundation, with the invisible local helper owning audio capture and playback. Preserve event observation and the polling hedge, but admit only final validated structured responses. Avoid regressions in final text, first device audio, barge-in, or total turn latency.

Current bridge/helper responsibilities that should remain:

- OpenCode server discovery or managed startup;
- Inception/Mercury provider config overlay;
- current session fork creation;
- fork cleanup;
- event-driven OpenCode structured-result observation via `prompt_async` and `/event`;
- polling as a non-cancelling hedge when event delivery stalls;
- context threshold monitoring;
- Mercury-backed compaction;
- invisible OS microphone capture;
- Deepgram Flux STT;
- Deepgram Speak/Aura TTS;
- barge-in;
- strict display/spoken response validation with one prompt-based repair and no legacy fallback;
- generic visual work activity, local onset/holding cues, and at most one deterministic spoken holding phrase.

### 15.2 Provider Configuration

For v1:

- Read `INCEPTION_API_KEY` from `.env` / environment.
- Use Mercury as the configured OpenCode provider/model path.
- Keep `DEEPGRAM_API_KEY` in `.env` / environment for STT/TTS.
- Do not build BYOK UI yet.
- Do not build a hosted proxy yet.

Future:

- BYOK user settings.
- Hosted proxy option that can serve managed keys to users.
- Organization policy controls.

### 15.3 Model/Runtime Disclosure

The sidepod should not report `Mercury`, `Inception`, `Deepgram`, model IDs, or runtime variants in normal UI.

Allowed places:

- logs;
- debug trace;
- config files;
- health endpoint;
- optional hidden developer panel.

Normal product UI should say:

- `Ready`
- `Listening`
- `Thinking`
- `Speaking`
- `Refreshing`
- `Voice Bridge Issue`

### 15.4 Active Thread and Fork Lifecycle

Because Mortic is inside an OpenCode thread sidebar:

1. The active OpenCode session is inferred.
2. Starting Mortic creates an ephemeral fork of that active session.
3. Voice turns run against the fork.
4. The source session remains untouched.
5. The fork is deleted by default when the voice session ends.
6. Handoff copy is the explicit mechanism for carrying selected voice work back to the source thread.

### 15.5 Context Compaction

Context compaction should remain backend-driven:

- threshold: 70k active context tokens;
- run in background when possible;
- use Mercury/Inception provider via `.env`;
- time compaction in logs;
- do not run compaction every turn unless threshold is truly crossed;
- surface only minimal UI status when compaction blocks a turn.

User-facing compaction statuses:

- `Preparing context`
- `Continuing`
- `Try again`

Avoid:

- token spam;
- provider/model labels;
- compaction implementation details.

## 16. Sidepod-to-Helper Contract

The sidepod should connect to the local bridge/native helper over HTTP/WebSocket. The sidepod should not capture microphone bytes itself. It sends control intent and renders state; the helper owns OS mic capture, Deepgram, TTS playback, OpenCode turns, barge-in, and filtering.

### 16.1 Health

Request:

```text
GET /api/health
```

Used for:

- helper availability;
- audio availability;
- basic readiness;
- debug-only configuration.

Normal UI should only use this to decide `Ready`, `Refreshing`, or `Voice Bridge Issue`.

### 16.2 Voice WebSocket

Endpoint:

```text
WS /ws/sidepod
```

Commands and events use protocol v0 exactly as defined in `docs/MORTIC_PROTOCOL_V0.md` and generated from `protocol/schema.ts`. The sidepod sends control intent only; browser PCM and browser-era control vocabulary are not supported product paths.

The TUI should translate these into product states, not display event names directly.

## 17. State Machine

```text
OpenCodePrompt
  ├─ /mortic -> Ready
  └─ normal typing -> OpenCode conversation

Unavailable
  └─ Refresh -> ConfirmRefresh

Connecting
  ├─ success -> Ready
  └─ failure -> Unavailable

Ready
  ├─ M down/ptt.start -> ListeningPTT
  ├─ L -> LiveListening
  ├─ T -> TranscriptPopup
  ├─ H -> HandoffPopup
  ├─ C -> ConfigStub
  ├─ Esc -> ConfirmExit
  └─ R -> ConfirmRefresh

ListeningPTT
  ├─ transcript update -> ListeningPTT
  ├─ M up/ptt.stop -> Thinking
  ├─ Esc/Stop -> ConfirmExit
  └─ error -> Error

LiveListening
  ├─ speech.end -> Thinking
  ├─ L -> Ready
  ├─ Esc -> ConfirmExit
  ├─ speech.start during TTS -> BargeIn
  └─ error -> Error

Thinking
  ├─ assistant.first_text -> Speaking
  ├─ turn.error -> Error
  └─ barge_in -> ListeningPTT or LiveListening

Speaking
  ├─ assistant.delta -> Speaking
  ├─ turn.complete -> Ready
  ├─ Esc -> ConfirmExit
  ├─ speech.start -> BargeIn
  └─ error -> Error

BargeIn
  └─ new speech captured -> ListeningPTT or LiveListening

TranscriptPopup
  ├─ C -> copy transcript
  └─ Esc -> previous state

HandoffPopup
  ├─ G -> generate handoff
  ├─ C -> copy handoff
  └─ Esc -> previous state

ConfigStub
  └─ Esc -> Ready

ConfirmRefresh
  ├─ Y -> Connecting
  └─ N/Esc -> previous Mortic state

ConfirmExit
  ├─ Y -> OpenCodePrompt
  └─ N/Esc -> previous Mortic state
```

Escape should not immediately exit Mortic focus. It should ask the user to confirm. On confirm, it should stop active voice capture/playback best-effort and reset Mortic focus. It should not delete the source OpenCode thread. Refresh should also ask for confirmation before resetting the ephemeral voice lane/fork.

Confirmation copy:

- `R`: `Exit current Mortic voice lane and start fresh? Y/N`
- `Esc`: `Exit Mortic and return to OpenCode prompt? Y/N`

## 18. Status and Error Copy

### 18.1 Preferred Copy

| Condition | Hero | COMMS |
| --- | --- | --- |
| Ready | `Ready` | `Ready for a spoken turn.` |
| PTT listening | `Listening` | live user transcript |
| Live enabled | `Live` | `Voice lane open.` |
| Waiting on OpenCode | `Thinking` | `Thinking...` |
| TTS playing | `Speaking` | assistant text streaming |
| Refresh running | `Refreshing` | `Starting a fresh voice lane.` |
| Voice bridge issue | `Voice Bridge Issue` | `Press R to retry.` |
| Mic blocked | `Mic needed` | `Allow microphone access for the helper.` |
| Turn interrupted | `Interrupted` | `Previous turn stopped.` |

### 18.2 Avoided Copy

Do not show:

- `inception/mercury-2`;
- `Mercury high`;
- `Deepgram Flux`;
- `OpenCode event stream failed`;
- raw WebSocket exception strings;
- raw JSON.

Unless debug mode is explicitly enabled.

## 19. Handoff and Transcript Data Model

The sidepod should maintain a local turn log for the current Mortic session:

```text
Turn
- id
- started_at
- ended_at
- source: ptt | live
- user_final_text
- assistant_spoken_text
- assistant_screen_only_hint
- interrupted
- error
- latency_first_text_ms
- latency_first_audio_ms
- latency_complete_ms
```

This log powers:

- COMMS current turn;
- Transcript popup;
- Handoff generation;
- observability;
- copy behavior.

The source OpenCode thread remains separate.

## 20. Observability

Logs should remain detailed for engineering, but UI should stay calm.

Required logs:

- bridge start;
- sidepod connect/disconnect;
- active session detection;
- fork create/delete;
- PTT start/stop;
- Live on/off;
- first transcript;
- first assistant text;
- first TTS audio;
- turn complete;
- barge-in;
- refresh/reset;
- compaction start/complete/error;
- handoff generate/copy.

Suggested metrics:

- time to first transcript;
- time from end-of-turn to first assistant text;
- time to first TTS audio;
- total turn latency;
- TTS chunk count;
- barge-in count;
- refresh/reset success/failure count;
- compaction latency;
- fork cleanup success/failure.

## 21. Privacy and Security

1. API keys must stay in `.env` / environment.
2. The TUI must never print keys.
3. Clipboard copy should only copy user-requested transcript/handoff text.
4. Raw provider responses should not appear in the sidepod.
5. Ephemeral forks should be deleted by default.
6. Handoff is explicit user-controlled copy, not automatic mutation of the source thread.
7. The packaged product should not expose a visible browser capture UI. OS microphone permission and capture state belong to the invisible helper/native layer.

## 22. Accessibility and Ergonomics

Mortic is keyboard-first:

- all core controls are reachable without mouse;
- command labels include key hints;
- focus mode is clear;
- popups are scrollable with common terminal keys;
- status is visible through text, not color alone;
- error states include a next action;
- labels stay short enough for narrow sidebars.

Mouse remains supported for discoverability, but it is secondary.

## 23. Acceptance Criteria

### 23.1 Product Acceptance

- User can focus Mortic from OpenCode without mouse.
- User can type `/mortic` to focus the Mortic sidepod without sending a model conversation turn.
- While Mortic is focused, ordinary typing does not advance the main OpenCode thread.
- Pressing `Esc` opens an exit confirmation before Mortic focus resets and returns to the OpenCode prompt.
- User can speak a PTT turn from the current thread without choosing a thread.
- User can toggle Live mode.
- User can press `R` to request Refresh, then confirm before the current ephemeral fork is deleted/discarded and a fresh one starts from the active OpenCode thread.
- User must confirm before `Esc` exits/resets Mortic focus.
- User can see a non-functional `C Config` stub in the command deck for future settings/subscriptions/API keys.
- User can open Transcript popup and scroll it.
- User can copy Transcript with `C`.
- User can open Handoff popup.
- User can generate and copy a handoff.
- COMMS shows recognition and work activity immediately, then the final validated response atomically.
- COMMS auto-scrolls/re-renders when text exceeds available space.
- Runtime/provider/model names are not shown in normal UI.
- Source OpenCode session is not mutated by voice interaction.
- Ephemeral fork is deleted by default after interaction.

### 23.2 Technical Acceptance

- Sidepod can connect to the invisible local bridge/native helper.
- Sidepod can start or discover the helper according to the implementation plan chosen later.
- Sidepod maps helper WebSocket events to UI states.
- Sidepod can send PTT/Live/Refresh/Config/Transcript/Handoff commands.
- `/mortic` slash command focuses the sidepod and is not forwarded as a normal model prompt.
- Mortic focus mode captures `M` while pressed so PTT does not leak into the OpenCode prompt or other keymaps.
- `R` and `Esc` confirmation prompts are implemented before reset/exit actions execute.
- Event-driven OpenCode structured-result observation remains the default backend path.
- Polling fallback remains available.
- Context compaction remains threshold-gated.
- Structured response validation prevents code/diffs/commands/paths/JSON and literal bracket notation from being spoken.
- Logs include latency fields needed for regression comparisons.

### 23.3 UX Acceptance

- Normal operation requires no mouse.
- User can tell whether Mortic is listening, thinking, or speaking.
- User can interrupt speech naturally.
- Transcript and Handoff are separate popups.
- Handoff summary is useful as a copyable prompt, not just a transcript dump.
- No confusing `last` or `items` labels appear in the command deck.

## 24. Implementation Milestones for Later

No development should happen until this PRD is reviewed and annotated.

Suggested milestones after approval:

1. Bridge connection proof
   - Sidepod reads bridge health.
   - Sidepod shows Ready/Voice Bridge Issue.
   - No voice controls yet.

2. Current thread and fork lifecycle
   - Sidepod resolves active OpenCode session.
   - Starts ephemeral fork.
   - Deletes fork by default.

3. Command deck wiring
   - PTT, Live, Refresh, `C Config` stub.
   - Keyboard-first focus mode.
   - `/mortic` slash command focus entrypoint.

4. COMMS live turn rendering
   - user transcript;
   - assistant event deltas;
   - auto-scroll/current turn display.

5. Transcript popup
   - turn log;
   - scroll;
   - copy.

6. Handoff popup
   - generate summary;
   - preview;
   - copy.

7. Voice polish
   - barge-in;
   - TTS chunk timing;
   - speech-only filtering;
   - status copy.

8. Aggressive testing
   - local TUI interaction;
   - helper unavailable and refresh/reset recovery;
   - Deepgram unavailable;
   - Inception key missing;
   - fork cleanup;
   - compaction threshold;
   - event stream fallback;
   - keyboard-only run.

## 25. Open Questions for Annotation

1. Should the canonical PTT key be `M` to match the old Electron app, with `P` as an alias, or should the native sidepod use `P` only?
2. Should Handoff generation happen locally through the helper, or through an OpenCode agent turn in the ephemeral fork?
3. Should the sidepod expose a hidden debug mode for provider/runtime details, or should those remain logs-only?
4. Should Config stub render disabled inline, or open a tiny placeholder popup?
5. Should Transcript include timing/latency metadata, or stay purely conversational?
6. Should Handoff include explicit "files touched" only when OpenCode reports file changes, or avoid file-level detail entirely?

## 26. Product Decisions Encoded in This Draft

1. Mortic is native to the current OpenCode thread.
2. No thread selection in the sidepod.
3. No normal UI runtime/model/provider reporting.
4. Command deck should not ship `last` or `items`.
5. COMMS is current-turn streaming UI, not full transcript.
6. Transcript and Handoff are separate scrollable popups.
7. Handoff is summary-first and copyable with `C`.
8. User should not need the mouse.
9. The first backend uses `.env` Inception and Deepgram keys.
10. Refresh is the only reset control; there is no separate Clear command in v1.
11. No typed fallback ships in the sidepod.
12. No visible browser UI ships in the packaged main path.
13. `C Config` exists as a non-functional stub in v1 for future API key, subscription, account, and routing settings.
14. `/mortic` is the preferred focus entrypoint.
15. Escape asks for confirmation before exiting/resetting Mortic focus and returning to the OpenCode prompt.
16. Refresh asks for confirmation before resetting the voice lane/fork.
17. Mortic focus mode captures `M` while pressed so PTT is isolated from other OpenCode keymaps.
18. BYOK/proxy are future product phases.

/**
 * Mortic Sidepod <-> Engine protocol v0 — normative schema.
 *
 * This file is the single source of truth for the wire contract documented in
 * docs/MORTIC_PROTOCOL_V0.md. Prose and fixtures follow this file; the JSON
 * artifacts both runtimes validate against are generated from it by
 * protocol/generate.ts (`npm run gen` here). Editing this file without
 * regenerating fails the hash guards in both test suites.
 *
 * Contract rules encoded here:
 * - Unknown fields on known message types are tolerated (all objects are loose).
 * - Unknown message types are NOT part of the unions; receivers log and ignore.
 * - Required/optional field sets match the protocol document exactly.
 */
import { z } from "zod";

export const PROTOCOL_VERSION = "mortic.sidepod.v0";

const isoTimestamp = z.string().min(1);
const id = z.string().min(1);

/** Fields every message carries. */
const eventBase = {
  sentAt: isoTimestamp,
};

/** Sidepod commands additionally carry a client correlation id. */
const commandBase = {
  ...eventBase,
  clientEventId: id,
};

/** Safe per-turn timing summary; all members best-effort. */
const latencySummary = z
  .looseObject({
    firstTranscriptMs: z.number().optional(),
    firstAssistantTextMs: z.number().optional(),
    firstAudioMs: z.number().optional(),
    totalMs: z.number().optional(),
  });

// ---------------------------------------------------------------------------
// Sidepod commands (sidepod -> engine)
// ---------------------------------------------------------------------------

export const startCommand = z.looseObject({
  ...commandBase,
  type: z.literal("start"),
  sourceSessionId: id,
  keepFork: z.boolean(),
  workspacePath: z.string().optional(),
  /** Local OpenCode server that owns the source thread (amendment 2026-07-04). */
  opencodeUrl: z.string().optional(),
  protocolVersion: z.literal(PROTOCOL_VERSION).optional(),
});

export const pttStartCommand = z.looseObject({
  ...commandBase,
  type: z.literal("ptt.start"),
  turnId: id,
  inputMode: z.literal("ptt"),
  key: z.string().optional(),
  eventType: z.string().optional(),
  terminalSupportsKeyRelease: z.boolean().optional(),
});

export const pttStopCommand = z.looseObject({
  ...commandBase,
  type: z.literal("ptt.stop"),
  turnId: id,
  reason: z.string(),
  matchingStartEventId: id.optional(),
  eventType: z.string().optional(),
});

export const liveSetCommand = z.looseObject({
  ...commandBase,
  type: z.literal("live.set"),
  value: z.boolean(),
  reason: z.string().optional(),
});

export const refreshCommand = z.looseObject({
  ...commandBase,
  type: z.literal("refresh"),
  reason: z.string(),
  voiceLaneId: id.optional(),
  sourceSessionId: id.optional(),
  confirmedByPromptId: id.optional(),
});

export const bargeInCommand = z.looseObject({
  ...commandBase,
  type: z.literal("barge_in"),
  reason: z.string(),
  turnId: id.optional(),
  voiceLaneId: id.optional(),
});

export const confirmResponseCommand = z.looseObject({
  ...commandBase,
  type: z.literal("confirm.response"),
  promptId: id,
  actionId: z.enum(["refresh", "exit"]),
  confirmed: z.boolean(),
  voiceLaneId: id.optional(),
});

/** Amendment 2026-07-04: explicit lane teardown (End Session, thread switch). */
export const stopCommand = z.looseObject({
  ...commandBase,
  type: z.literal("stop"),
  reason: z.string(),
  voiceLaneId: id.optional(),
});

// ---------------------------------------------------------------------------
// Engine events (engine -> sidepod)
// ---------------------------------------------------------------------------

export const readyEvent = z.looseObject({
  ...eventBase,
  type: z.literal("ready"),
  voiceLaneId: id,
  state: z.string(),
  sourceSessionId: id.optional(),
  forkSessionId: id.optional(),
  protocolVersion: z.literal(PROTOCOL_VERSION).optional(),
});

export const listeningEvent = z.looseObject({
  ...eventBase,
  type: z.literal("listening"),
  voiceLaneId: id,
  mode: z.enum(["ptt", "live"]),
  turnId: id.optional(),
});

export const transcriptEvent = z.looseObject({
  ...eventBase,
  type: z.literal("transcript"),
  turnId: id,
  sequence: z.int(),
  text: z.string(),
  final: z.boolean(),
  confidence: z.number().optional(),
  timing: z.looseObject({}).optional(),
});

export const thinkingEvent = z.looseObject({
  ...eventBase,
  type: z.literal("thinking"),
  turnId: id,
  sourceMode: z.enum(["ptt", "live"]),
  voiceLaneId: id.optional(),
  submittedTextChars: z.int().optional(),
});

export const assistantDeltaEvent = z.looseObject({
  ...eventBase,
  type: z.literal("assistant.delta"),
  turnId: id,
  sequence: z.int(),
  delta: z.string(),
  screenOnly: z.boolean().optional(),
});

export const speakingEvent = z.looseObject({
  ...eventBase,
  type: z.literal("speaking"),
  turnId: id,
  voiceLaneId: id.optional(),
  firstAudioLatencyMs: z.number().optional(),
});

export const completeEvent = z.looseObject({
  ...eventBase,
  type: z.literal("complete"),
  turnId: id,
  latency: latencySummary,
  fullSpokenText: z.string().optional(),
  tokenSummary: z.looseObject({}).optional(),
  streamSource: z.enum(["event", "poll", "poll_after_event"]).optional(),
});

export const interruptedEvent = z.looseObject({
  ...eventBase,
  type: z.literal("interrupted"),
  reason: z.string(),
  turnId: id.optional(),
  voiceLaneId: id.optional(),
});

export const voiceBridgeIssueEvent = z.looseObject({
  ...eventBase,
  type: z.literal("voice_bridge_issue"),
  userMessage: z.string(),
  diagnosticCode: z.string(),
  retryable: z.boolean(),
  safeDetail: z.string().optional(),
  voiceLaneId: id.optional(),
  debugRef: z.string().optional(),
});

/** Amendment 2026-07-04: acknowledged lane teardown, pairs with `stop`. */
export const stoppedEvent = z.looseObject({
  ...eventBase,
  type: z.literal("stopped"),
  reason: z.string(),
  forkDeleted: z.boolean(),
  voiceLaneId: id.optional(),
});

// ---------------------------------------------------------------------------
// Registries
// ---------------------------------------------------------------------------

export const COMMANDS = {
  start: startCommand,
  "ptt.start": pttStartCommand,
  "ptt.stop": pttStopCommand,
  "live.set": liveSetCommand,
  refresh: refreshCommand,
  barge_in: bargeInCommand,
  "confirm.response": confirmResponseCommand,
  stop: stopCommand,
} as const;

export const EVENTS = {
  ready: readyEvent,
  listening: listeningEvent,
  transcript: transcriptEvent,
  thinking: thinkingEvent,
  "assistant.delta": assistantDeltaEvent,
  speaking: speakingEvent,
  complete: completeEvent,
  interrupted: interruptedEvent,
  voice_bridge_issue: voiceBridgeIssueEvent,
  stopped: stoppedEvent,
} as const;

export type SidepodCommand = z.infer<(typeof COMMANDS)[keyof typeof COMMANDS]>;
export type EngineEvent = z.infer<(typeof EVENTS)[keyof typeof EVENTS]>;

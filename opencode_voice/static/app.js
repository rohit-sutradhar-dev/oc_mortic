const state = {
  socket: null,
  audioContext: null,
  mediaStream: null,
  processor: null,
  micActive: false,
  nextPlaybackTime: 0,
  playbackSources: [],
  sampleRate: 16000,
};

const els = {
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  sessionSelect: document.getElementById("sessionSelect"),
  refreshBtn: document.getElementById("refreshBtn"),
  keepFork: document.getElementById("keepFork"),
  startBtn: document.getElementById("startBtn"),
  stopBtn: document.getElementById("stopBtn"),
  micBtn: document.getElementById("micBtn"),
  modelText: document.getElementById("modelText"),
  tokenText: document.getElementById("tokenText"),
  compactionText: document.getElementById("compactionText"),
  ttsText: document.getElementById("ttsText"),
  transcript: document.getElementById("transcript"),
  assistant: document.getElementById("assistant"),
  textForm: document.getElementById("textForm"),
  textPrompt: document.getElementById("textPrompt"),
  sendTextBtn: document.getElementById("sendTextBtn"),
  opencodeFrame: document.getElementById("opencodeFrame"),
};

window.addEventListener("load", init);
els.refreshBtn.addEventListener("click", loadSessions);
els.startBtn.addEventListener("click", startFork);
els.stopBtn.addEventListener("click", stopFork);
els.micBtn.addEventListener("click", toggleMic);
els.keepFork.addEventListener("change", () => send({ type: "keep_fork", value: els.keepFork.checked }));
els.textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = els.textPrompt.value.trim();
  if (!text) return;
  append(els.transcript, `You: ${text}\n`);
  send({ type: "text", text });
  els.textPrompt.value = "";
});

async function init() {
  await loadHealth();
  await loadSessions();
  connectSocket();
}

async function loadHealth() {
  const response = await fetch("/api/health");
  const data = await response.json();
  els.modelText.textContent = data.model;
  els.statusText.textContent = data.opencode?.healthy ? "OpenCode ready" : "OpenCode unavailable";
  els.statusDot.className = data.opencode?.healthy ? "dot ready" : "dot error";
  els.opencodeFrame.src = data.opencode_url;
  state.sampleRate = data.deepgram?.sample_rate || 16000;
  els.ttsText.textContent = data.deepgram?.enabled ? data.deepgram.tts_model : "No key";
}

async function loadSessions() {
  const response = await fetch("/api/sessions");
  const data = await response.json();
  els.sessionSelect.textContent = "";
  for (const session of data.sessions) {
    if (session.is_voice_tmp) continue;
    const option = document.createElement("option");
    option.value = session.id;
    option.textContent = `${session.title} (${formatNumber(session.context_tokens)} tokens)`;
    option.dataset.tokens = session.context_tokens;
    els.sessionSelect.appendChild(option);
  }
  updateSelectedTokens();
}

function connectSocket() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  state.socket = new WebSocket(`${protocol}//${location.host}/ws/voice`);
  state.socket.binaryType = "arraybuffer";
  state.socket.addEventListener("open", () => {
    els.statusText.textContent = "Voice bridge ready";
    els.statusDot.className = "dot ready";
  });
  state.socket.addEventListener("message", handleSocketMessage);
  state.socket.addEventListener("close", () => {
    els.statusText.textContent = "Voice bridge closed";
    els.statusDot.className = "dot error";
    setTimeout(connectSocket, 1500);
  });
}

async function handleSocketMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    playPcm(event.data);
    return;
  }
  const message = JSON.parse(event.data);
  switch (message.type) {
    case "ready":
      break;
    case "fork.ready":
      els.statusText.textContent = "Fork ready";
      els.tokenText.textContent = formatNumber(message.context_tokens);
      els.startBtn.disabled = true;
      els.stopBtn.disabled = false;
      els.micBtn.disabled = false;
      els.sendTextBtn.disabled = false;
      append(els.transcript, `Fork: ${message.fork_session_id}\n`);
      break;
    case "tokens":
      els.tokenText.textContent = formatNumber(message.context_tokens);
      break;
    case "speech.start":
      append(els.transcript, "Listening\n");
      break;
    case "speech.transcript":
      if (message.transcript) append(els.transcript, `${message.transcript}\n`);
      break;
    case "turn.start":
      append(els.transcript, `You: ${message.text}\n`);
      append(els.assistant, "\nMercury: ");
      break;
    case "assistant.delta":
      append(els.assistant, message.delta);
      break;
    case "assistant.first_text":
      els.statusText.textContent = `Mercury first text ${message.latency_ms}ms`;
      break;
    case "turn.complete":
      append(els.assistant, "\n");
      els.statusText.textContent = `Turn complete ${message.latency_ms}ms`;
      break;
    case "turn.error":
      append(els.assistant, `\n${message.message}\n`);
      els.statusText.textContent = "Turn error";
      els.statusDot.className = "dot error";
      break;
    case "compaction.start":
      els.compactionText.textContent = `Running ${formatNumber(message.before_tokens)}`;
      break;
    case "compaction.complete":
      els.compactionText.textContent = `${message.latency_ms}ms`;
      els.tokenText.textContent = formatNumber(message.after_tokens);
      break;
    case "compaction.error":
      els.compactionText.textContent = "Error";
      break;
    case "tts.first_audio":
      els.ttsText.textContent = "Speaking";
      break;
    case "tts.skipped":
      els.ttsText.textContent = "No key";
      break;
    case "barge_in":
      stopPlayback();
      els.ttsText.textContent = "Interrupted";
      break;
    case "stopped":
      els.startBtn.disabled = false;
      els.stopBtn.disabled = true;
      els.micBtn.disabled = true;
      els.sendTextBtn.disabled = true;
      els.statusText.textContent = "Stopped";
      await loadSessions();
      break;
    case "error":
      els.statusText.textContent = message.message;
      els.statusDot.className = "dot error";
      break;
    default:
      break;
  }
}

function startFork() {
  const sessionId = els.sessionSelect.value;
  if (!sessionId) return;
  send({ type: "start", session_id: sessionId, keep_fork: els.keepFork.checked });
}

async function stopFork() {
  if (state.micActive) await stopMic();
  send({ type: "stop" });
  stopPlayback();
}

async function toggleMic() {
  if (state.micActive) {
    await stopMic();
  } else {
    await startMic();
  }
}

async function startMic() {
  if (!state.audioContext) state.audioContext = new AudioContext();
  state.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const source = state.audioContext.createMediaStreamSource(state.mediaStream);
  state.processor = state.audioContext.createScriptProcessor(4096, 1, 1);
  state.processor.onaudioprocess = (event) => {
    if (!state.micActive || state.socket?.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    state.socket.send(floatToPcm16(downsample(input, state.audioContext.sampleRate, state.sampleRate)));
  };
  source.connect(state.processor);
  state.processor.connect(state.audioContext.destination);
  state.micActive = true;
  els.micBtn.classList.add("active");
  send({ type: "audio.start" });
}

async function stopMic() {
  state.micActive = false;
  els.micBtn.classList.remove("active");
  send({ type: "audio.stop" });
  if (state.processor) state.processor.disconnect();
  if (state.mediaStream) {
    for (const track of state.mediaStream.getTracks()) track.stop();
  }
  state.processor = null;
  state.mediaStream = null;
}

function send(payload) {
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(payload));
  }
}

function append(element, text) {
  element.textContent += text;
  element.scrollTop = element.scrollHeight;
}

function updateSelectedTokens() {
  const selected = els.sessionSelect.selectedOptions[0];
  els.tokenText.textContent = selected ? formatNumber(selected.dataset.tokens) : "-";
}

els.sessionSelect.addEventListener("change", updateSelectedTokens);

function formatNumber(value) {
  const number = Number(value || 0);
  return new Intl.NumberFormat().format(number);
}

function downsample(buffer, inputRate, outputRate) {
  if (inputRate === outputRate) return buffer;
  const ratio = inputRate / outputRate;
  const length = Math.round(buffer.length / ratio);
  const result = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(Math.floor((i + 1) * ratio), buffer.length);
    let sum = 0;
    for (let j = start; j < end; j += 1) sum += buffer[j];
    result[i] = sum / Math.max(1, end - start);
  }
  return result;
}

function floatToPcm16(buffer) {
  const output = new Int16Array(buffer.length);
  for (let i = 0; i < buffer.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, buffer[i]));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output.buffer;
}

function playPcm(arrayBuffer) {
  if (!state.audioContext) state.audioContext = new AudioContext({ sampleRate: state.sampleRate });
  const pcm = new Int16Array(arrayBuffer);
  const audioBuffer = state.audioContext.createBuffer(1, pcm.length, state.sampleRate);
  const channel = audioBuffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  const source = state.audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(state.audioContext.destination);
  const startAt = Math.max(state.audioContext.currentTime + 0.02, state.nextPlaybackTime);
  source.start(startAt);
  state.nextPlaybackTime = startAt + audioBuffer.duration;
  state.playbackSources.push(source);
  source.onended = () => {
    state.playbackSources = state.playbackSources.filter((item) => item !== source);
    if (!state.playbackSources.length) els.ttsText.textContent = "Idle";
  };
}

function stopPlayback() {
  for (const source of state.playbackSources) {
    try {
      source.stop();
    } catch {
      // Source may already have ended.
    }
  }
  state.playbackSources = [];
  if (state.audioContext) state.nextPlaybackTime = state.audioContext.currentTime;
}

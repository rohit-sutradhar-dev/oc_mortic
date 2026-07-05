from __future__ import annotations

"""Audio-domain echo check for pending barge-ins.

The text gates classify a transcript against what the assistant *said*; this
module classifies the microphone signal against what the speaker *played*.
We retain a few seconds of post-AEC mic audio and of the render reference
(the exact PCM handed to the output device), and on a pending barge-in
compare their short-time energy envelopes. Echo that leaked past the
canceller keeps the render's energy contour (shifted by the acoustic path,
attenuated); a human interrupting produces an unrelated contour, and a human
talking OVER playback degrades the correlation instead of matching it — the
mixed-voice case fails safe toward "real interrupt".

numpy is safe as a direct import: the livekit wheel (a hard dependency, it
provides the echo canceller) requires numpy>=1.26 itself.
"""

import time
from collections import deque

import numpy as np

from opencode_voice.audio_processing import BYTES_PER_SAMPLE


class PcmRingBuffer:
    """Rolling window of timestamped PCM frames.

    `direction` sets what a frame's timestamp means: "ending" for mic frames
    (stamped on arrival, so the frame covers the preceding duration) and
    "starting" for render chunks (stamped when written to the device, so the
    frame covers the following duration). Appends may come from a worker
    thread (the render path); deque appends are atomic under the GIL and the
    reader tolerates a torn view, so no lock is needed.
    """

    def __init__(self, sample_rate: int, max_sec: float = 4.0, direction: str = "ending") -> None:
        self.sample_rate = sample_rate
        self.max_sec = max_sec
        self.direction = direction
        self.frames: deque[tuple[float, bytes]] = deque()

    def append(self, data: bytes, at: float | None = None) -> None:
        if not data:
            return
        now = time.perf_counter() if at is None else at
        self.frames.append((now, data))
        cutoff = now - self.max_sec
        while self.frames and self.frames[0][0] < cutoff:
            self.frames.popleft()

    def extract(self, start: float, end: float) -> bytes:
        chunks: list[bytes] = []
        for stamp, data in list(self.frames):
            duration = len(data) / (self.sample_rate * BYTES_PER_SAMPLE)
            if self.direction == "ending":
                frame_start, frame_end = stamp - duration, stamp
            else:
                frame_start, frame_end = stamp, stamp + duration
            if frame_end >= start and frame_start <= end:
                chunks.append(data)
        return b"".join(chunks)


def band_envelopes(pcm: bytes, sample_rate: int, hop_ms: int = 20, bands: int = 8) -> np.ndarray:
    """Log-spaced spectral band energies per hop — a coarse spectrogram.

    A single loudness envelope is too weak a fingerprint: syllable-rate
    amplitude modulation is quasi-periodic, so a best-lag search finds a
    spurious alignment between two UNRELATED speech segments (measured 0.93+
    on synthetic speech-like signals). Echo has to match the render across
    frequency bands simultaneously, which independent speech does not.
    """
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    hop = max(1, int(sample_rate * hop_ms / 1000))
    steps = len(samples) // hop
    if steps == 0:
        return np.zeros((0, bands), dtype=np.float32)
    frames = samples[: steps * hop].reshape(steps, hop) * np.hanning(hop)
    spectrum = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(hop, 1 / sample_rate)
    edges = np.geomspace(100, sample_rate / 2, bands + 1)
    out = np.zeros((steps, bands), dtype=np.float32)
    for band in range(bands):
        selected = (freqs >= edges[band]) & (freqs < edges[band + 1])
        if selected.any():
            out[:, band] = spectrum[:, selected].mean(axis=1)
    return np.log1p(out)


def _trim_silent_edges(envelope: np.ndarray) -> np.ndarray:
    """Drop leading/trailing near-silent hops. Silence at the edges of the
    shorter segment poisons every alignment — the slide can move the window
    but not the silent prefix inside it — so an acoustic-delay gap before
    the echo starts would sink an otherwise perfect match."""
    if len(envelope) == 0:
        return envelope
    energy = envelope.sum(axis=1)
    active = np.where(energy > energy.max() * 0.1)[0]
    if len(active) == 0:
        return envelope[:0]
    return envelope[active[0] : active[-1] + 1]


def echo_correlation(
    mic_pcm: bytes,
    render_pcm: bytes,
    sample_rate: int,
    hop_ms: int = 20,
    min_overlap_sec: float = 0.4,
) -> float:
    """Peak normalized cross-correlation between the mic's and render's
    band-envelope spectrograms across all alignments. ~1.0 means the mic
    heard what we played (echo); independent or overlapping speech stays
    low. Returns 0.0 when either segment is too short to judge.
    """
    mic = band_envelopes(mic_pcm, sample_rate, hop_ms)
    render = band_envelopes(render_pcm, sample_rate, hop_ms)
    min_hops = max(2, int(min_overlap_sec * 1000 / hop_ms))
    short, long_ = (mic, render) if len(mic) <= len(render) else (render, mic)
    short = _trim_silent_edges(short)
    if len(short) < min_hops or len(long_) < min_hops or len(short) > len(long_):
        return 0.0
    short = short - short.mean(axis=0, keepdims=True)
    short_norm = float(np.linalg.norm(short))
    if short_norm == 0.0:
        return 0.0
    best = 0.0
    for lag in range(0, len(long_) - len(short) + 1):
        window = long_[lag : lag + len(short)]
        window = window - window.mean(axis=0, keepdims=True)
        denom = float(np.linalg.norm(window)) * short_norm
        if denom == 0.0:
            continue
        best = max(best, float((short * window).sum()) / denom)
    return best

"""Audio feedback sounds for recording start/stop/error events.

Generates simple sine-wave WAV tones programmatically, caches them under
``~/.speakflow/sounds/``, and plays them asynchronously via macOS ``afplay``
so the main thread is never blocked.  All errors are silenced -- sound
feedback is a nice-to-have, never a hard requirement.
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
import threading
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SOUNDS_DIR = Path.home() / ".speakflow" / "sounds"

_SAMPLE_RATE = 44100
_AMPLITUDE = 0.6  # 0.0 .. 1.0 -- keep it comfortable

# Tone definitions: (filename, start_hz, end_hz, duration_seconds)
_TONE_START = ("start.wav", 440, 880, 0.15)
_TONE_STOP = ("stop.wav", 880, 440, 0.15)
_TONE_ERROR = ("error.wav", 200, 200, 0.30)

# ---------------------------------------------------------------------------
# WAV generation helpers
# ---------------------------------------------------------------------------


def _generate_tone(
    path: Path,
    freq_start: int,
    freq_end: int,
    duration: float,
) -> None:
    """Write a mono 16-bit WAV file containing a sine sweep (or fixed tone)."""
    n_frames = int(_SAMPLE_RATE * duration)
    max_val = 32767  # 16-bit signed max
    fade_frames = int(_SAMPLE_RATE * 0.01)  # 10 ms fade-in/out to avoid clicks

    phase = 0.0
    samples = bytearray()
    for i in range(n_frames):
        # Linear frequency interpolation for the sweep.
        progress = i / n_frames
        freq = freq_start + (freq_end - freq_start) * progress

        # Advance phase by the instantaneous frequency.
        phase += 2 * math.pi * freq / _SAMPLE_RATE
        value = _AMPLITUDE * math.sin(phase)

        # Apply fade-in / fade-out envelope.
        if i < fade_frames:
            value *= i / fade_frames
        elif i > n_frames - fade_frames:
            value *= (n_frames - i) / fade_frames

        samples += struct.pack("<h", int(value * max_val))

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(bytes(samples))


_sounds_ready = False

def _ensure_sounds() -> None:
    """Generate cached WAV files if they don't already exist."""
    global _sounds_ready
    if _sounds_ready:
        return
    _SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, freq_start, freq_end, duration in (
        _TONE_START,
        _TONE_STOP,
        _TONE_ERROR,
    ):
        path = _SOUNDS_DIR / filename
        if not path.exists():
            _generate_tone(path, freq_start, freq_end, duration)
    _sounds_ready = True


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


_volume: float = 0.5  # 0.0 .. 1.0 — controlled via set_volume()


def set_volume(vol: float) -> None:
    """Set playback volume (0.0–1.0)."""
    global _volume
    _volume = max(0.0, min(1.0, vol))


def get_volume() -> float:
    return _volume


def _play(filename: str) -> None:
    """Play a cached WAV file asynchronously via macOS ``afplay``."""

    vol = _volume  # snapshot for the thread

    def _worker() -> None:
        try:
            _ensure_sounds()
            path = _SOUNDS_DIR / filename
            if not path.exists():
                return
            subprocess.run(
                ["/usr/bin/afplay", "-v", str(vol), str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def warm_up() -> None:
    """Pre-generate sound files in a background thread."""
    threading.Thread(target=_ensure_sounds, daemon=True).start()


def play_start_sound() -> None:
    _play(_TONE_START[0])


def play_stop_sound() -> None:
    _play(_TONE_STOP[0])


def play_error_sound() -> None:
    _play(_TONE_ERROR[0])

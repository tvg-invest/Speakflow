"""Audio recording module for SpeakFlow.

Captures microphone audio using sounddevice, with silence detection
and WAV output optimised for Whisper (16 kHz, mono, 16-bit PCM).
"""
from __future__ import annotations

import io
import logging
import threading
import time
import wave

logger = logging.getLogger(__name__)

import numpy as np
import sounddevice as sd


class AudioRecorderError(Exception):
    """Base exception for audio recording errors."""


class NoMicrophoneError(AudioRecorderError):
    """Raised when no input device is available."""


class PermissionDeniedError(AudioRecorderError):
    """Raised when microphone access is denied by the OS."""


class AudioRecorder:
    """Records microphone audio in a background thread.

    Parameters
    ----------
    sample_rate : int
        Sampling rate in Hz. Default 16 000 (optimal for Whisper).
    channels : int
        Number of audio channels. Default 1 (mono).
    silence_timeout : float
        Seconds of continuous silence before *on_silence_detected* fires.
    max_duration : float
        Hard cap on recording length in seconds.
    silence_threshold_factor : float
        Multiplier applied to the ambient noise floor to derive the
        silence threshold. Lower values make detection more sensitive.
    """

    # Duration (in seconds) of the calibration window used to measure
    # ambient noise at the start of each recording.
    _CALIBRATION_WINDOW: float = 0.5

    # Size of each audio chunk captured from the device, in frames.
    _CHUNK_FRAMES: int = 1024

    # Absolute floor for the silence threshold so that a perfectly
    # quiet calibration period does not make detection impossible.
    _MIN_SILENCE_THRESHOLD: float = 1e-4

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        silence_timeout: float = 2.0,
        max_duration: float = 7200,
        silence_threshold_factor: float = 1.5,
        device=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.silence_timeout = silence_timeout
        self.max_duration = max_duration
        self.silence_threshold_factor = silence_threshold_factor
        self.device = device  # None = system default, or int device index

        # Public callbacks -- set by the caller.
        self.on_silence_detected: callable | None = None
        self.on_error: callable | None = None  # Called with (error_msg: str)
        self.on_max_duration: callable | None = None

        # Live audio level (updated every chunk, read by UI for visualisation).
        self.current_rms: float = 0.0

        # Internal state ------------------------------------------------
        self._recording = False
        self._frames: list[np.ndarray] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Silence detection state
        self._silence_threshold: float = 0.0
        self._silence_start: float | None = None
        self._silence_triggered = False
        self._calibration_rms_values: list[float] = []
        self._calibrated = False
        self._recording_start_time: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def list_input_devices() -> list[dict]:
        """Return a list of available input devices as {id, name} dicts."""
        devices = sd.query_devices()
        if isinstance(devices, dict):
            devices = [devices]
        result = []
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                result.append({"id": i, "name": d["name"]})
        return result

    @property
    def is_recording(self) -> bool:
        """True while the recorder is actively capturing audio."""
        return self._recording

    def start_recording(self) -> None:
        """Begin capturing audio from the default input device.

        Returns immediately; audio is captured on a background thread.

        Raises
        ------
        AudioRecorderError
            If already recording.
        NoMicrophoneError
            If no input device can be found.
        PermissionDeniedError
            If the OS blocks microphone access.
        """
        if self._recording:
            raise AudioRecorderError("Recording is already in progress.")

        # Verify that an input device exists before we spin up a thread.
        self._check_input_device()

        self._reset_state()
        self._recording = True
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._record_loop,
            name="speakflow-audio",
            daemon=True,
        )
        self._thread.start()

    def stop_recording(self) -> bytes:
        """Stop recording and return captured audio as WAV bytes.

        Returns
        -------
        bytes
            A complete WAV file (RIFF header + PCM data) in memory.
            If the recording already stopped (e.g. max duration reached),
            returns whatever audio was captured.
        """
        if not self._recording:
            # Already stopped (max duration or error) — return captured audio
            with self._lock:
                frames = list(self._frames)
            if not frames:
                return self._empty_wav()
            audio = np.concatenate(frames, axis=0)
            return self._to_wav_bytes(audio)

        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        self._recording = False

        with self._lock:
            frames = list(self._frames)

        if not frames:
            return self._empty_wav()

        audio = np.concatenate(frames, axis=0)
        return self._to_wav_bytes(audio)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        """Clear buffers and detection state for a fresh recording."""
        with self._lock:
            self._frames = []
        self._silence_threshold = 0.0
        self._silence_start = None
        self._silence_triggered = False
        self._calibration_rms_values = []
        self._calibrated = False
        self._recording_start_time = 0.0

    def _check_input_device(self) -> None:
        """Ensure a usable input device is available."""
        try:
            devices = sd.query_devices()
        except Exception as exc:
            raise NoMicrophoneError(
                "Unable to query audio devices."
            ) from exc

        # sd.query_devices() may return a single DeviceList or dict.
        if isinstance(devices, dict):
            devices = [devices]

        has_input = any(
            d.get("max_input_channels", 0) > 0 for d in devices
        )
        if not has_input:
            raise NoMicrophoneError(
                "No microphone or audio input device found."
            )

    _MAX_OPEN_RETRIES: int = 3
    _RETRY_DELAY: float = 0.3

    def _record_loop(self) -> None:
        """Background thread: open an input stream and collect chunks.

        Retries opening the audio device up to ``_MAX_OPEN_RETRIES`` times
        to recover from transient PortAudio errors (e.g. after sleep/wake).
        """
        self._recording_start_time = time.monotonic()

        max_duration_reached = False
        try:
            stream = self._open_stream_with_retry()
            with stream:
                while not self._stop_event.is_set():
                    elapsed = time.monotonic() - self._recording_start_time
                    if elapsed >= self.max_duration:
                        max_duration_reached = True
                        break

                    data, overflowed = stream.read(self._CHUNK_FRAMES)
                    chunk = data.copy()

                    with self._lock:
                        self._frames.append(chunk)

                    self._process_chunk(chunk)

        except sd.PortAudioError as exc:
            err_msg = str(exc).lower()
            if "permission" in err_msg or "not allowed" in err_msg:
                msg = ("Microphone access was denied. Check System Settings > "
                       "Privacy & Security > Microphone.")
            else:
                msg = f"Audio device error: {exc}"
            if self.on_error:
                try:
                    self.on_error(msg)
                except Exception:
                    pass
        except Exception as exc:
            msg = f"Unexpected error during recording: {exc}"
            if self.on_error:
                try:
                    self.on_error(msg)
                except Exception:
                    pass
        finally:
            self._recording = False
            if max_duration_reached and self.on_max_duration is not None:
                try:
                    self.on_max_duration()
                except Exception:
                    logger.warning("on_max_duration callback error", exc_info=True)

    def _open_stream_with_retry(self) -> sd.InputStream:
        """Try to open the audio input stream, retrying on transient errors."""
        last_exc = None
        for attempt in range(self._MAX_OPEN_RETRIES):
            try:
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    blocksize=self._CHUNK_FRAMES,
                    device=self.device,
                )
                stream.start()
                return stream
            except sd.PortAudioError as exc:
                last_exc = exc
                if attempt < self._MAX_OPEN_RETRIES - 1:
                    time.sleep(self._RETRY_DELAY)
        raise last_exc

    # ------------------------------------------------------------------
    # Silence detection
    # ------------------------------------------------------------------

    @staticmethod
    def _rms(chunk: np.ndarray) -> float:
        """Return the root-mean-square energy of an int16 audio chunk."""
        return float(np.sqrt(np.mean(chunk.astype(np.int64) ** 2))) / 32768.0

    def _process_chunk(self, chunk: np.ndarray) -> None:
        """Analyse a chunk for calibration / silence detection."""
        rms = self._rms(chunk)
        self.current_rms = rms
        elapsed = time.monotonic() - self._recording_start_time

        if not self._calibrated:
            self._calibration_rms_values.append(rms)
            if elapsed >= self._CALIBRATION_WINDOW:
                self._finalise_calibration()
            return

        # After calibration, run silence detection.
        if rms < self._silence_threshold:
            if self._silence_start is None:
                self._silence_start = time.monotonic()
            elif (
                not self._silence_triggered
                and (time.monotonic() - self._silence_start)
                >= self.silence_timeout
            ):
                self._silence_triggered = True
                if self.on_silence_detected is not None:
                    try:
                        self.on_silence_detected()
                    except Exception:
                        logger.warning(
                            "on_silence_detected callback error", exc_info=True)
        else:
            # Sound detected -- reset the silence timer.
            self._silence_start = None
            self._silence_triggered = False

    def _finalise_calibration(self) -> None:
        """Derive the silence threshold from the calibration window.

        Uses the 25th percentile of collected RMS values rather than the
        mean so that speech captured during the calibration window does
        not inflate the ambient-noise estimate.
        """
        if self._calibration_rms_values:
            ambient_rms = float(
                np.percentile(self._calibration_rms_values, 25)
            )
        else:
            ambient_rms = 0.0

        self._silence_threshold = max(
            ambient_rms * self.silence_threshold_factor,
            self._MIN_SILENCE_THRESHOLD,
        )
        self._calibrated = True

    # ------------------------------------------------------------------
    # WAV encoding
    # ------------------------------------------------------------------

    def _to_wav_bytes(self, audio: np.ndarray) -> bytes:
        """Encode an int16 ndarray as an in-memory WAV file."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()

    def _empty_wav(self) -> bytes:
        """Return a valid but zero-length WAV file."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(b"")
        return buf.getvalue()

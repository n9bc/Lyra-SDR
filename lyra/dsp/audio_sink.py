"""Audio output sinks: where demodulated audio goes.

Two implementations:
- AK4951Sink: packs audio into EP2 TX slots on the HL2 stream; the
  updated gateware routes these samples to the AK4951 codec line-out.
- SoundDeviceSink: outputs to PC default playback device via sounddevice
  (soft dependency; only imported when this sink is selected).
"""
from __future__ import annotations

from typing import Optional, Protocol

import numpy as np


class AudioSink(Protocol):
    def write(self, audio: np.ndarray) -> None: ...
    def close(self) -> None: ...


class AK4951Sink:
    """Route audio to the HL2's AK4951 line-level output via EP2 TX slots.

    Sink-swap cleanup: the underlying HL2Stream owns a TX audio
    queue (deque) that's NOT per-sink — it's a long-lived buffer
    shared across sink swaps. We clear it on both init AND close,
    so swapping to/from this sink doesn't leak stale samples between
    sessions ("digitized robotic" symptom: old samples + new samples
    interleaved in the EP2 frames).
    """

    def __init__(self, stream):
        self._stream = stream
        # Drain any leftover TX audio from a previous session before
        # we start enqueuing fresh samples.
        if hasattr(stream, "clear_tx_audio"):
            stream.clear_tx_audio()
        self._stream.inject_audio_tx = True

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        self._stream.queue_tx_audio(audio.astype(np.float32))

    def close(self) -> None:
        self._stream.inject_audio_tx = False
        # Clear the queue on close so the NEXT sink (PC Soundcard
        # or another AK4951 instance) starts from a known empty
        # state. Without this, residual samples in the deque continue
        # being pulled by EP2 framing for up to ~1 second.
        if hasattr(self._stream, "clear_tx_audio"):
            self._stream.clear_tx_audio()


class SoundDeviceSink:
    """Route audio to the PC default playback device.

    Key design choices (documented because they matter for Windows
    audio interfaces, USB multichannel cards, and S/PDIF outputs):

    - **Prefers WASAPI over MME.** PortAudio's system default on
      Windows is MME (20+ years old, flaky with S/PDIF and USB audio
      interfaces, silently drops mono frames on some drivers). We
      explicitly pick the WASAPI host API's default output device
      when the caller didn't specify one. WASAPI is what every
      serious audio app (DAWs, Thetis, ExpertSDR3, browsers) uses.

    - **Opens stereo, writes duplicated mono.** The demod chain is
      mono (SSB/CW/AM/FM/DIGU all produce a single audio channel).
      S/PDIF / TOSLINK outputs are rigidly 2-channel and some drivers
      silently drop mono frames instead of auto-duplicating — so we
      always open stereo and duplicate the mono sample into both L
      and R. Harmless on analog outputs (which would have duplicated
      anyway).
    """

    def __init__(self, rate: int = 48000, device: Optional[int] = None,
                 blocksize: int = 1024):
        try:
            import sounddevice as sd
        except ImportError as e:
            raise RuntimeError(
                "sounddevice is not installed. `pip install sounddevice` "
                "or switch the audio output to AK4951."
            ) from e
        self._sd = sd
        self._rate = rate

        if device is None:
            device = self._pick_wasapi_default(sd)

        self._channels = 2
        self._stream = sd.OutputStream(
            samplerate=rate, channels=self._channels, dtype="float32",
            blocksize=blocksize, device=device,
        )
        self._stream.start()

    @staticmethod
    def _pick_wasapi_default(sd):
        """Find the WASAPI host API's default output device. Returns a
        device index, or None if WASAPI isn't available (falls through
        to PortAudio's system default — probably MME on Windows, which
        is less reliable but not always broken).
        """
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            return None
        for i, ha in enumerate(hostapis):
            if ha["name"] == "Windows WASAPI":
                default_out = ha.get("default_output_device", -1)
                if default_out >= 0:
                    return default_out
                return None
        return None

    def write(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        mono = audio.astype(np.float32).reshape(-1)
        # Duplicate mono to (N, 2) stereo — mandatory for S/PDIF,
        # harmless on analog.
        a = np.stack((mono, mono), axis=1)
        try:
            self._stream.write(a)
        except self._sd.PortAudioError:
            # Intentionally swallowed: a transient PortAudio error
            # (e.g., device exclusive-mode grabbed by another app)
            # should not crash the audio thread. If the user ever
            # reports "no audio" with a clean stream, re-enable the
            # diagnostic prints in the git history for this file.
            pass

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class NullSink:
    def write(self, audio): pass
    def close(self): pass

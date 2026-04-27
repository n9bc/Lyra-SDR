"""Demodulation primitives — stateful FIR-based, artifact-free.

Using scipy.signal.lfilter with maintained state across blocks to avoid
the FFT block-edge artifacts (motorboating/ticking) that a naive
block-by-block FFT filter produces.

Sideband convention: on this HL2, positive baseband frequencies
correspond to what users hear as LSB (the spectrum is effectively
mirrored relative to the tuned frequency — likely a gateware or I/Q
decode artifact). The demod classes apply the empirically-correct sign
so the "USB"/"LSB" mode labels match operator expectations.
"""
from __future__ import annotations

import numpy as np

try:
    from scipy.signal import firwin, lfilter
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False


class SSBDemod:
    """Single-sideband demodulation (USB or LSB) with complex FIR bandpass.

    `low_hz` and `high_hz` control the audio passband.  The filter is
    sharper when taps is larger, but taps should stay odd for symmetry.
    """

    def __init__(self, rate: int, mode: str = "USB",
                 low_hz: float = 300.0, high_hz: float = 2700.0,
                 taps: int = 255):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy is required; run: pip install scipy")
        self.rate = rate
        self.mode = mode
        f_center = (low_hz + high_hz) / 2.0
        half_bw = (high_hz - low_hz) / 2.0
        lpf = firwin(taps, half_bw, fs=rate, window="hann")
        n = np.arange(taps) - (taps - 1) / 2.0
        # HL2 baseband spectrum is mirrored relative to the standard
        # convention: USB RF signals land in NEGATIVE baseband freqs on
        # this gateware. Confirmed empirically on 40m FT8 (N8SDR 2026-04-21):
        # user had to select "LSB" in prior code to hear USB-transmitted FT8.
        # A bandpass centered at -f_center is built via lpf * exp(-j*ω*n).
        sign = -1.0 if mode == "USB" else +1.0
        phasor = np.exp(sign * 1j * 2 * np.pi * f_center * n / rate)
        self.coeffs = (lpf * phasor).astype(np.complex64)
        self.state = np.zeros(taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        out, self.state = lfilter(self.coeffs, 1.0, iq, zi=self.state)
        # Factor 2 compensates for keeping only one sideband
        return (np.real(out) * 2.0).astype(np.float32)


class CWDemod:
    """CW — SSB-style narrow filter, flush against the carrier marker.

    Architecturally identical to SSBDemod, just with a narrower
    passband (250..1500 Hz typical instead of 2400). CWU passes
    0..BW Hz above the carrier, CWL passes 0..BW Hz below. Same
    HL2 baseband-mirror convention as SSBDemod.

    The CW pitch knob controls where INSIDE the filter the operator
    places the signal via click-to-tune, which determines the
    audible tone:
      pitch=650, BW=800 → click on CW signal, marker drops 650 Hz
        below the signal (CWU), signal lands at +650 baseband, heard
        as a 650 Hz audio tone.
      pitch=400 → same click action puts marker 400 Hz below, signal
        at +400 baseband → 400 Hz tone.

    For pitch to be audible the BW must be at least pitch + a small
    skirt. Default BW for CW is therefore 800 Hz. Operator can narrow
    further (down to ~50 Hz) for crowded contests, in which case
    they'd typically lower the pitch as well.
    """

    def __init__(self, rate: int, pitch_hz: float = 650.0,
                 bw_hz: float = 800.0, taps: int = 513,
                 sideband: str = "U"):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.pitch_hz = float(pitch_hz)  # advisory; used by panel for click offset
        self.bw_hz = float(bw_hz)
        self.taps = int(taps)
        self.sideband = sideband
        self._build_filter()
        self.state = np.zeros(self.taps - 1, dtype=np.complex64)

    def _build_filter(self) -> None:
        """Complex bandpass spanning 0..BW from the carrier, on the
        side of the carrier dictated by sideband. Mirrors SSBDemod's
        HL2 baseband convention: USB → filter at NEGATIVE baseband."""
        low_hz = 0.0
        high_hz = max(50.0, self.bw_hz)
        f_center = (low_hz + high_hz) / 2.0
        half_bw = (high_hz - low_hz) / 2.0
        proto = firwin(self.taps, half_bw, fs=self.rate,
                       window="hann").astype(np.float64)
        n = np.arange(self.taps) - (self.taps - 1) / 2.0
        # Same convention as SSBDemod: USB sits at NEGATIVE baseband
        # on the HL2 gateware.
        sign = -1.0 if self.sideband.upper().startswith("U") else +1.0
        phasor = np.exp(sign * 1j * 2.0 * np.pi * f_center * n / self.rate)
        self.lpf = (proto * phasor).astype(np.complex64)

    def set_pitch_hz(self, pitch_hz: float) -> None:
        """Pitch is advisory for CWDemod (filter is flush against
        marker, not centered on pitch). Stored so external callers
        can read it back; no filter rebuild needed."""
        self.pitch_hz = float(pitch_hz)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        out, self.state = lfilter(self.lpf, 1.0, iq, zi=self.state)
        return (np.real(out) * 2.0).astype(np.float32)


class DSBDemod:
    """Double-sideband suppressed-carrier AM.

    Real part of a bandpass-filtered I/Q gives both sidebands summed.
    Requires a carrier to be present at DC (baseband); if carrier is
    absent, use SAM or carrier-restore AM modes instead.
    """

    def __init__(self, rate: int, bw_hz: float = 5000.0, taps: int = 255):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.lpf = firwin(taps, bw_hz / 2.0, fs=rate,
                          window="hann").astype(np.float64)
        self.state = np.zeros(taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        filt, self.state = lfilter(self.lpf, 1.0, iq, zi=self.state)
        return np.real(filt).astype(np.float32) * 2.0


class FMDemod:
    """Narrow-band FM via phase discriminator.

    audio(t) ∝ arg( iq(t) * conj(iq(t-1)) )
    Followed by de-emphasis LPF. Default deviation 5 kHz (typical NBFM
    on 10 m / 2 m repeaters in HF ranges where HL2 operates).
    """

    def __init__(self, rate: int, deviation_hz: float = 5000.0,
                 audio_bw_hz: float = 3000.0, taps: int = 129):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.deviation = deviation_hz
        self.lpf = firwin(taps, audio_bw_hz, fs=rate,
                          window="hann").astype(np.float64)
        self.state = np.zeros(taps - 1, dtype=np.float32)
        self._prev = np.complex64(1 + 0j)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        # Shift by one sample across block boundary
        shifted = np.empty_like(iq)
        shifted[0] = self._prev
        shifted[1:] = iq[:-1]
        self._prev = iq[-1]
        # Phase difference → instantaneous frequency
        disc = np.angle(iq * np.conj(shifted))
        # Scale so ±deviation maps to ±1.0
        audio_raw = (disc * self.rate / (2 * np.pi * self.deviation)).astype(np.float32)
        # De-emphasis LPF
        filtered, self.state = lfilter(self.lpf, 1.0, audio_raw, zi=self.state)
        return filtered.astype(np.float32)


class NotchFilter:
    """Stateful IIR notch — removes a narrow band of frequencies from
    complex I/Q before demod. Applied real-valued to I and Q separately
    so the notch is symmetric around DC (perfect for killing a carrier
    or CW interference near baseband).

    Parameter is **width_hz** (notch -3 dB bandwidth in Hz), not Q.
    Operators think in absolute width ("kill a 100 Hz wide chunk")
    not in Q values — the SDR-client convention operators expect.
    Internally the iirnotch design uses Q = freq / width.

    Two filter modes selected by `freq_hz` proximity to DC:

    - **Off-DC** (default for any non-zero freq): scipy `iirnotch`.
      Narrow band-stop centered on `freq_hz`, bandwidth = `width_hz`.
      Right tool for off-DC heterodynes, FT8 tones, RTTY pairs.

    - **DC blocker** (`dc_blocker=True`): butterworth high-pass with
      corner at `width_hz / 2`. Used when the operator clicks at/near
      VFO center (the WWV-on-carrier case) — iirnotch's bandwidth
      = freq/Q collapses as freq approaches 0, so it can't catch DC.
      The high-pass kills DC + everything below the corner
      symmetrically on both sides of baseband.

    Either way, the rendered "notch region" on the spectrum spans
    `freq_hz ± width_hz/2`.
    """

    def __init__(self, rate: int, freq_hz: float, width_hz: float,
                 dc_blocker: bool = False, deep: bool = False):
        """Build a stateful notch.

        `deep=True` cascades the same biquad twice in sequence, which
        roughly DOUBLES the dB attenuation at every frequency offset
        (and tightens the −3 dB BW by ~30%). Costs 2× the per-block
        compute and 2× the transient settling time on a fresh state.
        Useful for stubborn carriers where a single iirnotch leaks 3-6
        dB at the edges of the kill region.
        """
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy required")
        self.rate = rate
        self.freq_hz = freq_hz
        self.width_hz = width_hz
        self.dc_blocker = dc_blocker
        self.deep = deep
        if dc_blocker:
            from scipy.signal import butter
            # High-pass corner at width/2 so the visible notch extent
            # (freq ± width/2 → 0..width) matches what the operator
            # sees on the spectrum overlay. 4th order: steep enough
            # that the corner is well-defined without ringing.
            corner = max(width_hz * 0.5, 5.0)
            self.b, self.a = butter(4, corner, btype='high', fs=rate)
        else:
            from scipy.signal import iirnotch
            # iirnotch parameter Q = center / -3dB-bandwidth.
            q = max(freq_hz / max(width_hz, 0.5), 0.5)
            w0 = freq_hz / (rate / 2.0)
            self.b, self.a = iirnotch(w0, q)
        order = max(len(self.a), len(self.b)) - 1
        self.state_i = np.zeros(order, dtype=np.float32)
        self.state_q = np.zeros(order, dtype=np.float32)
        # Second-stage state for the cascade. Only allocated when
        # `deep=True` so normal notches stay cheap.
        if self.deep:
            self.state2_i = np.zeros(order, dtype=np.float32)
            self.state2_q = np.zeros(order, dtype=np.float32)

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return iq
        i_out, self.state_i = lfilter(self.b, self.a, iq.real, zi=self.state_i)
        q_out, self.state_q = lfilter(self.b, self.a, iq.imag, zi=self.state_q)
        if self.deep:
            # Second pass with its own state — cascading the same
            # filter twice in sequence. Doubles the dB attenuation
            # at every offset.
            i_out, self.state2_i = lfilter(
                self.b, self.a, i_out, zi=self.state2_i)
            q_out, self.state2_q = lfilter(
                self.b, self.a, q_out, zi=self.state2_q)
        return (i_out + 1j * q_out).astype(np.complex64)


class AMDemod:
    """AM envelope detection with LPF and DC removal."""

    def __init__(self, rate: int, bw_hz: float = 5000.0, taps: int = 129):
        if not _HAVE_SCIPY:
            raise RuntimeError("scipy is required; run: pip install scipy")
        self.rate = rate
        self.lpf = firwin(taps, bw_hz, fs=rate, window="hann").astype(np.float64)
        self.state = np.zeros(taps - 1, dtype=np.complex64)
        self._dc = 0.0

    def process(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)
        filtered, self.state = lfilter(self.lpf, 1.0, iq, zi=self.state)
        env = np.abs(filtered).astype(np.float32)
        # Simple one-pole DC removal (slow enough to track AM carrier only)
        block_mean = float(np.mean(env))
        self._dc = 0.95 * self._dc + 0.05 * block_mean
        return (env - self._dc).astype(np.float32)


# Legacy one-shot functions kept for backward compatibility with existing
# tools/tests. Not used by the live app — they have block-edge artifacts.
def usb_demod(iq: np.ndarray, rate: int,
              low_hz: float = 300.0, high_hz: float = 2700.0) -> np.ndarray:
    d = SSBDemod(rate, "USB", low_hz, high_hz)
    return d.process(iq.astype(np.complex64))


def lsb_demod(iq: np.ndarray, rate: int,
              low_hz: float = 300.0, high_hz: float = 2700.0) -> np.ndarray:
    d = SSBDemod(rate, "LSB", low_hz, high_hz)
    return d.process(iq.astype(np.complex64))


def am_demod(iq: np.ndarray, rate: int, bw_hz: float = 5000.0) -> np.ndarray:
    d = AMDemod(rate, bw_hz)
    return d.process(iq.astype(np.complex64))

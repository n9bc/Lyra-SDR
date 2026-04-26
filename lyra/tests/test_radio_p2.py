"""Tests for Radio class Protocol 2 integration (Phase 7).

Verifies:
  1. Radio.discover() sets _protocol='P2' and _board_id=10 when
     discover_all() returns a P2 ANAN-G2.
  2. Radio.start() instantiates P2Stream (not HL2Stream) with
     board_id=10 when _protocol='P2'.
  3. Radio.discover() sets _protocol='P1' for Hermes-class radios.
  4. _board_id is stored and forwarded even after discover + start
     roundtrip.

All network I/O is stubbed — no real radio or Qt display required.
"""
from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Stub Qt before lyra.radio imports it.  We do this at module load time so
# the import chain (lyra.radio → PySide6) never sees the real library.
# ---------------------------------------------------------------------------

def _make_qt_stub():
    """Return a minimal MagicMock that satisfies lyra.radio's Qt needs."""
    sig = MagicMock()
    sig.return_value = MagicMock(emit=MagicMock(), connect=MagicMock())

    qt_core = MagicMock()
    qt_core.QObject = object        # inherit from plain object
    qt_core.Signal = sig            # Signal(…) returns a MagicMock
    qt_core.QTimer = MagicMock()

    pyside6 = MagicMock()
    pyside6.QtCore = qt_core
    return pyside6, qt_core


_pyside6_stub, _qtcore_stub = _make_qt_stub()

# Inject stubs before any lyra.radio import touches sys.modules
for _mod_name in ("PySide6", "PySide6.QtCore", "PySide6.QtWidgets",
                  "PySide6.QtGui"):
    sys.modules.setdefault(_mod_name, _pyside6_stub)
sys.modules["PySide6.QtCore"] = _qtcore_stub

# Also stub heavy scientific deps that radio.py pulls at import time
for _mod_name in ("scipy", "scipy.signal"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()


# ---------------------------------------------------------------------------
# Now we can safely import lyra.radio
# ---------------------------------------------------------------------------

import lyra.radio as _radio_mod   # noqa: E402  (must be after sys.modules setup)
from lyra.radio import Radio       # noqa: E402


# ---------------------------------------------------------------------------
# Fake discovery objects
# ---------------------------------------------------------------------------

@dataclass
class _FakeP2RadioInfo:
    ip: str = "192.168.10.206"
    mac: str = "DE:AD:BE:EF:00:01"
    board_id: int = 10
    board_name: str = "OrionMKII"
    code_version: int = 5
    is_busy: bool = False


@dataclass
class _FakeP1RadioInfo:
    ip: str = "10.10.30.100"
    mac: str = "AA:BB:CC:DD:EE:FF"
    board_id: int = 6
    board_name: str = "HermesLite"
    code_version: int = 76
    is_busy: bool = False
    beta_version: int = 0


@dataclass
class _FakeDiscoveredRadio:
    protocol: str
    raw: object

    @property
    def ip(self):
        return self.raw.ip

    @property
    def mac(self):
        return self.raw.mac

    @property
    def board_name(self):
        return self.raw.board_name

    @property
    def board_id(self):
        return self.raw.board_id

    @property
    def code_version(self):
        return self.raw.code_version

    @property
    def is_busy(self):
        return self.raw.is_busy


def _p2_radio():
    return _FakeDiscoveredRadio(protocol="P2", raw=_FakeP2RadioInfo())


def _p1_radio():
    return _FakeDiscoveredRadio(protocol="P1", raw=_FakeP1RadioInfo())


# ---------------------------------------------------------------------------
# Lightweight stub object that has only the fields Radio.discover() touches.
# We call the unbound Radio.discover(stub) so we test the actual method body
# without constructing a full Radio (which needs a running QApplication).
# ---------------------------------------------------------------------------

class _DiscoverStub:
    def __init__(self):
        self._protocol = "P1"
        self._board_id = None
        self._ip = "10.10.30.100"
        self._status_msgs = []

    def set_ip(self, ip):
        self._ip = ip

    # Fake signal with .emit()
    class _Signal:
        def emit(self, *a): pass
    status_message = _Signal()


class _StartStub:
    """Minimal object with the attributes Radio.start() reads/writes."""
    def __init__(self, protocol="P2", board_id=10, ip="192.168.10.206"):
        self._protocol = protocol
        self._board_id = board_id
        self._ip = ip
        self._rate = 192_000
        self._freq_hz = 14_107_000
        self._gain_db = 20
        self._stream = None
        self._filter_board_enabled = False
        self._audio_sink = MagicMock()

    def _make_sink(self):
        return MagicMock()

    def _push_balance_to_sink(self):
        pass

    def _apply_oc_for_current_freq(self):
        pass

    class _Signal:
        def emit(self, *a): pass
    status_message = _Signal()
    stream_state_changed = _Signal()

    class _FakeTimer:
        def start(self, *a): pass
        def stop(self): pass
    _peak_report_timer = _FakeTimer()
    _hl2_telem_timer = _FakeTimer()


# ---------------------------------------------------------------------------
# Tests: Radio.discover() with P2 radio
# ---------------------------------------------------------------------------

class TestRadioDiscoverP2(unittest.TestCase):

    def _run_discover(self, radios):
        stub = _DiscoverStub()
        with patch("lyra.radio.discover_all",
                   return_value=radios):
            Radio.discover(stub)
        return stub

    def test_p2_radio_sets_protocol(self):
        stub = self._run_discover([_p2_radio()])
        self.assertEqual(stub._protocol, "P2")

    def test_p2_radio_stores_board_id(self):
        stub = self._run_discover([_p2_radio()])
        self.assertEqual(stub._board_id, 10)

    def test_p2_radio_sets_ip(self):
        stub = self._run_discover([_p2_radio()])
        self.assertEqual(stub._ip, "192.168.10.206")

    def test_p1_radio_sets_protocol(self):
        stub = self._run_discover([_p1_radio()])
        self.assertEqual(stub._protocol, "P1")

    def test_p1_radio_stores_board_id_6(self):
        stub = self._run_discover([_p1_radio()])
        self.assertEqual(stub._board_id, 6)

    def test_empty_discovery_keeps_default_protocol(self):
        stub = self._run_discover([])
        self.assertEqual(stub._protocol, "P1")

    def test_empty_discovery_keeps_default_board_id(self):
        stub = self._run_discover([])
        self.assertIsNone(stub._board_id)

    def test_first_radio_wins_protocol(self):
        stub = self._run_discover([_p2_radio(), _p1_radio()])
        self.assertEqual(stub._protocol, "P2")

    def test_first_radio_wins_board_id(self):
        stub = self._run_discover([_p2_radio(), _p1_radio()])
        self.assertEqual(stub._board_id, 10)


# ---------------------------------------------------------------------------
# Tests: Radio.start() instantiates P2Stream with correct board_id
# ---------------------------------------------------------------------------

class TestRadioStartP2(unittest.TestCase):

    def test_p2_start_passes_board_id_10(self):
        """The critical Phase 7 check: board_id=10 reaches P2Stream."""
        stub = _StartStub(protocol="P2", board_id=10)
        captured_kwargs = []
        mock_instance = MagicMock()
        mock_instance.stats = MagicMock(frames=0, samples=0, seq_errors=0)

        def fake_p2stream(ip, sample_rate, *, board_id=None):
            captured_kwargs.append({"ip": ip, "sample_rate": sample_rate,
                                    "board_id": board_id})
            return mock_instance

        with patch.object(_radio_mod, "P2Stream", fake_p2stream):
            Radio.start(stub)

        self.assertEqual(len(captured_kwargs), 1,
                         "P2Stream should be constructed exactly once")
        self.assertEqual(captured_kwargs[0]["board_id"], 10,
                         f"Expected board_id=10, got {captured_kwargs[0]['board_id']}")

    def test_p2_start_ip_forwarded(self):
        stub = _StartStub(protocol="P2", board_id=10, ip="192.168.10.206")
        captured = []
        mock_instance = MagicMock()
        mock_instance.stats = MagicMock(frames=0)

        def fake_p2stream(ip, sample_rate, *, board_id=None):
            captured.append(ip)
            return mock_instance

        with patch.object(_radio_mod, "P2Stream", fake_p2stream):
            Radio.start(stub)

        self.assertEqual(captured[0], "192.168.10.206")

    def test_p2_board_id_none_does_not_raise(self):
        """board_id=None is valid — P2Stream defaults to DDC0."""
        stub = _StartStub(protocol="P2", board_id=None)
        mock_instance = MagicMock()
        mock_instance.stats = MagicMock(frames=0)

        def fake_p2stream(ip, sample_rate, *, board_id=None):
            return mock_instance

        with patch.object(_radio_mod, "P2Stream", fake_p2stream):
            Radio.start(stub)  # should not raise

    def test_p1_uses_hl2stream_not_p2stream(self):
        """P1 radios must never instantiate P2Stream."""
        stub = _StartStub(protocol="P1", board_id=6, ip="10.10.30.100")
        p2stream_called = []
        mock_hl2 = MagicMock()
        mock_hl2.return_value.stats = MagicMock(frames=0)

        def fake_p2stream(*a, **kw):
            p2stream_called.append(True)
            return MagicMock()

        with patch.object(_radio_mod, "HL2Stream", mock_hl2), \
             patch.object(_radio_mod, "P2Stream", fake_p2stream):
            Radio.start(stub)

        self.assertFalse(p2stream_called, "P2Stream must not be called for P1 radio")
        mock_hl2.assert_called_once()

    def test_p1_hl2stream_gets_correct_ip(self):
        stub = _StartStub(protocol="P1", board_id=6, ip="10.10.30.100")
        mock_hl2 = MagicMock()
        mock_hl2.return_value.stats = MagicMock(frames=0)

        with patch.object(_radio_mod, "HL2Stream", mock_hl2):
            Radio.start(stub)

        self.assertEqual(mock_hl2.call_args[0][0], "10.10.30.100")


# ---------------------------------------------------------------------------
# Tests: _board_id field present and defaults to None
# ---------------------------------------------------------------------------

class TestBoardIdInit(unittest.TestCase):

    def test_board_id_attribute_in_init(self):
        """Radio.__init__ must initialise _board_id."""
        import inspect
        src = inspect.getsource(Radio.__init__)
        self.assertIn("_board_id", src)

    def test_board_id_default_is_none(self):
        import inspect
        src = inspect.getsource(Radio.__init__)
        # Matches both `self._board_id = None` and
        # `self._board_id: Optional[int] = None`
        self.assertTrue(
            "self._board_id = None" in src or
            "self._board_id: Optional[int] = None" in src,
            "_board_id initialiser not found in Radio.__init__",
        )

    def test_protocol_attribute_in_init(self):
        import inspect
        src = inspect.getsource(Radio.__init__)
        self.assertIn("_protocol", src)

    def test_protocol_default_is_p1(self):
        import inspect
        src = inspect.getsource(Radio.__init__)
        self.assertIn('"P1"', src)


# ---------------------------------------------------------------------------
# Tests: board_id roundtrip (discover stores → start uses)
# ---------------------------------------------------------------------------

class TestBoardIdRoundtrip(unittest.TestCase):

    def test_discover_then_start_passes_board_id(self):
        """End-to-end: discover a P2 radio, then start() passes its board_id."""
        # Step 1: discover
        disc_stub = _DiscoverStub()
        with patch("lyra.radio.discover_all",
                   return_value=[_p2_radio()]):
            Radio.discover(disc_stub)

        self.assertEqual(disc_stub._board_id, 10)

        # Step 2: build a start stub seeded with the discovered state
        start_stub = _StartStub(
            protocol=disc_stub._protocol,
            board_id=disc_stub._board_id,
            ip=disc_stub._ip,
        )

        captured = []
        mock_instance = MagicMock()
        mock_instance.stats = MagicMock(frames=0)

        def fake_p2stream(ip, sample_rate, *, board_id=None):
            captured.append(board_id)
            return mock_instance

        with patch.object(_radio_mod, "P2Stream", fake_p2stream):
            Radio.start(start_stub)

        self.assertEqual(captured[0], 10,
                         "board_id from discovery must reach P2Stream")


if __name__ == "__main__":
    unittest.main()

"""Lyra — Qt6 SDR transceiver for Hermes Lite 2 / 2+.

Version policy
--------------
Pre-1.0 releases use 0.<minor>.<patch> where:
  - <minor> bumps for user-facing feature batches
  - <patch> bumps for bug-fix-only releases between feature batches

The single source of truth is `__version__` below. UI surfaces (title
bar, About dialog, status bar, settings dialog footer) and the install
guide all read from this string — bump it once here per release and
everything else follows automatically.
"""
__version__      = "0.0.2"
__version_name__ = "Banner & Telemetry"
# Build date is filled in at release time by the packaging script.
# During development it stays "dev" so devs don't accidentally ship a
# stale date. The tester-build (PyInstaller) workflow will rewrite
# this constant to today's date as part of the release pipeline.
__build_date__   = "dev"


def version_string() -> str:
    """Human-readable version string for UI / log display.

    Format:
        '0.0.2 — Banner & Telemetry (dev build)'    (development)
        '0.0.2 — Banner & Telemetry  (2026-04-25)'  (release)
    """
    base = f"{__version__} — {__version_name__}"
    if __build_date__ == "dev":
        return f"{base} (dev build)"
    return f"{base}  ({__build_date__})"

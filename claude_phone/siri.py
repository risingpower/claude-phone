"""Siri voices, which `say` can't use. Download one in System Settings → Accessibility → Spoken Content →
System Voice → Manage Voices. macOS gives Siri voices only to Apple-signed processes, so siri.swift runs under
the `swift` interpreter (Xcode command line tools): about a second per render, and the TTS cache hides it."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).with_name("siri.swift")


def _swift(*args: str, text: str = "") -> str:
    r = subprocess.run(["swift", str(SCRIPT), *args], input=text, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"Siri voice failed: {r.stderr.strip() or r.returncode}")
    return r.stdout


def voices() -> list[tuple[str, str, str]]:
    """Installed Siri voices as (identifier, name, language)."""
    return [tuple(line.split("\t")) for line in _swift("--list").splitlines() if line]


def render(text: str, voice: str | None, out: Path) -> None:
    """Speak text into out (CAF). voice: an identifier, a name ("Voice 2") or a language ("en-GB").
    Empty: the first Siri voice in the Mac's language."""
    _swift(voice or "", str(out), text=text)

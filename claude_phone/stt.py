"""Speech to text via a resident whisper-server (model stays loaded: ~0.3 s per clip vs 5 s cold)."""

from __future__ import annotations

import array
import re
import wave
from pathlib import Path

import aiohttp

from . import config, log, registry

# Phrases whisper produces from near-silence.
_NOISE = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|thank you\.?|thanks for watching!?|you|\.+)?\s*$", re.I)


# T31P handset speech can peak at RMS ~550; line silence sits well under 100.
def speech_seconds(path: Path, threshold: int = 200) -> float:
    """Rough voiced duration: seconds of 20 ms frames whose RMS exceeds threshold."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            return 1.0  # unknown format: let whisper decide
        rate = w.getframerate()
        a = array.array("h", w.readframes(w.getnframes()))
    step = rate // 50
    voiced = 0
    for i in range(0, len(a) - step, step):
        chunk = a[i:i + step]
        rms = (sum(x * x for x in chunk) / step) ** 0.5
        if rms > threshold:
            voiced += 1
    return voiced / 50


def vocabulary(extra: str = "") -> str:
    """Prompt whisper with live repo and branch names (and any extra words expected) so it spells them right."""
    words: set[str] = set()
    for s in registry.all_sessions():
        for part in (s.repo, s.branch, s.worktree):
            if part:
                words.add(part.replace("-", " ").replace("/", " "))
    listed = [*sorted(words), *([extra] if extra else [])]
    return ("Claude Code. " + ", ".join(listed) + ".") if listed else "Claude Code."


async def transcribe(path: Path, extra: str = "") -> str:
    """Returns the transcript, or '' when nothing was said."""
    lg = log.get("stt")
    voiced = speech_seconds(path)
    if voiced < 0.1:
        lg.info("no speech in %s (voiced %.2fs)", path.name, voiced)
        return ""
    s = config.load()
    form = aiohttp.FormData()
    form.add_field("file", path.read_bytes(), filename="audio.wav", content_type="audio/wav")
    form.add_field("response_format", "json")
    form.add_field("temperature", "0.0")
    form.add_field("prompt", vocabulary(extra))
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{s.whisper_url}/inference", data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            text = (await r.json())["text"]
    text = " ".join(text.split())
    if _NOISE.match(text):
        lg.info("discarded noise transcript %r", text)
        return ""
    return text

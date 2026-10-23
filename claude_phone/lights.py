"""Govee lights over Govee's LAN API (free, no account): switch LAN Control on for each light in the Govee app.
The operator takes spoken commands ("lights off", "red", "party mode"); every light gets the same command.
Effects (a pulse while Claude rings, a party when the safe is cracked) are short and put the lights back after;
the daemon skips them during meetings, when the lamps stay on normal white because they're in shot."""

from __future__ import annotations

import asyncio
import json
import random
import re
import socket
import time
from dataclasses import dataclass

from . import config, log

lg = log.get("lights")

GAP = 0.3  # seconds between commands to a lamp
SCAN_GROUP, SCAN_PORT, REPLY_PORT, CMD_PORT = "239.255.255.250", 4001, 4002, 4003

COLOURS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "purple": (140, 0, 255),
    "pink": (255, 40, 150), "orange": (255, 90, 0), "yellow": (255, 200, 0), "cyan": (0, 255, 255),
    "teal": (0, 180, 140), "magenta": (255, 0, 255),
}
# name -> (brightness, colour or kelvin, what the operator says)
MODES = {
    "normal": (20, 4780, "Back to normal."),
    "focus": (100, 6500, "Focus mode. Bright and cool."),
    "relax": (40, 2700, "Relax mode. Nice and warm."),
    "sunset": (60, (255, 90, 0), "Sunset mode."),
    "movie": (5, 2700, "Movie mode. Lights down."),
}
MODE_WORDS = {
    "normal": "normal", "usual": "normal", "default": "normal", "work": "normal", "working": "normal",
    "focus": "focus", "concentrate": "focus", "relax": "relax", "chill": "relax", "calm": "relax",
    "sunset": "sunset",
    "movie": "movie", "cinema": "movie", "film": "movie",
}
PARTY_WORDS = {"party", "disco", "rave"}
LIGHT_WORDS = {"light", "lights", "lamp", "lamps", "lamb", "lambs"}  # whisper hears "lamps" as "lambs" on a phone line
NUMBERS = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
           "eighty": 80, "ninety": 90, "hundred": 100}
RING_COLOUR = (255, 90, 30)  # Claude orange, as near as the lamps get
VOCABULARY = "lights on, lights off, " + ", ".join(sorted({*COLOURS, *MODE_WORDS, *PARTY_WORDS}))


@dataclass(frozen=True)
class Command:
    kind: str  # on | off | colour | kelvin | brightness | brighter | dimmer | mode | party
    value: object = None
    said: str = ""


def mentions_lights(text: str) -> bool:
    return bool(set(re.findall(r"[a-z]+", text.lower())) & LIGHT_WORDS)


def finished(text: str) -> bool:
    """ "No", "that's all", "nothing else": the caller is done."""
    t = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    t = re.sub(r"\s*\b(thanks|thank you|cheers|bye|goodbye)$", "", t).strip() or "thanks"
    return bool(re.fullmatch(r"(no|nope|nah|no that's (it|all)|that's (it|all)|nothing( else)?.*|all done|i'm done"
                             r"|done|thanks)", t))


def parse(text: str) -> Command | None:
    """A spoken request -> a lights command, or None if it isn't one. "On" and "off" need a light word, so
    "put me through to the frontend" isn't mistaken for a command; colours and modes don't."""
    t = text.lower().replace("-", " ")
    words = re.findall(r"[a-z]+|\d+", t)
    ws = set(words)
    lightish = bool(ws & LIGHT_WORDS)
    if ws & PARTY_WORDS:
        return Command("party", said="Party time!")
    mode = next((MODE_WORDS[w] for w in words if w in MODE_WORDS), None)
    if mode:
        return Command("mode", mode, MODES[mode][2])
    colour = next((w for w in words if w in COLOURS), None)
    if colour:
        return Command("colour", COLOURS[colour], f"{colour.capitalize()} it is.")
    if ws & {"warm", "warmer", "cosy", "cozy"}:
        return Command("kelvin", 2700, "Warm white.")
    if ws & {"cool", "cooler", "daylight", "white"}:
        return Command("kelvin", 6500, "Cool white.")
    pct = re.search(r"(\d{1,3})\s*(?:%|percent)", t)
    if pct or "percent" in ws:
        n = int(pct.group(1)) if pct else next((NUMBERS[w] for w in words if w in NUMBERS), None)
        if n is not None:
            n = max(1, min(100, n))
            return Command("brightness", n, f"{n} percent.")
    if ws & {"half"}:
        return Command("brightness", 50, "Half brightness.")
    if ws & {"full", "max", "maximum", "brightest"}:
        return Command("brightness", 100, "Full brightness.")
    if ws & {"brighter", "up"} and lightish:
        return Command("brighter", said="Brighter.")
    if ws & {"dim", "dimmer", "darker", "down", "low"} and (lightish or ws & {"dim", "dimmer", "darker"}):
        return Command("dimmer", said="Dimmer.")
    if lightish and ws & {"off", "out"}:
        return Command("off", said="Lights off.")
    if lightish and ws & {"on"}:
        return Command("on", said="Lights on.")
    return None


def _scan(lan_ip: str, wait: float = 2.0) -> list[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", REPLY_PORT))
        if lan_ip:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan_ip))
        s.settimeout(0.3)
        msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
        found: list[str] = []
        end, sent = time.monotonic() + wait, 0
        while time.monotonic() < end:
            if sent < 3:
                s.sendto(msg, (SCAN_GROUP, SCAN_PORT))
                sent += 1
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            ip = json.loads(data).get("msg", {}).get("data", {}).get("ip") or addr[0]
            if ip not in found:
                found.append(ip)
        return sorted(found)
    finally:
        s.close()


class Lights:
    def __init__(self, s: config.Settings):
        self.s = s
        self.ips: list[str] = []
        self.party: asyncio.Task | None = None  # party mode or an effect; any command stops it
        self.cache = config.HOME / "lights.json"

    async def find(self, fresh: bool = False) -> list[str]:
        """The lights' addresses: scanned once, then cached (also on disk, for when a scan comes back empty)."""
        if self.ips and not fresh:
            return self.ips
        try:
            ips = await asyncio.to_thread(_scan, self.s.lan_ip)
        except OSError as e:
            lg.warning("light scan failed: %r", e)
            ips = []
        if ips:
            self.cache.write_text(json.dumps(ips))
        else:
            try:
                ips = json.loads(self.cache.read_text())
            except (OSError, ValueError):
                ips = []
        self.ips = ips
        lg.info("lights: %s", ips or "none found")
        return ips

    def _send(self, ip: str, cmd: str, data: dict) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            if self.s.lan_ip:
                s.bind((self.s.lan_ip, 0))
            s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, CMD_PORT))

    async def _all(self, cmd: str, data: dict, ips: list[str] | None = None) -> None:
        """Sent twice: it's UDP with no acknowledgement and a lamp now and then misses one. Every command sets a
        value outright, so a repeat is harmless. GAP after each: an H607C drops a command that arrives within
        a moment of the last (brightness then white: the white was lost 3 times in 3; 0.3 s apart, 0 in 3)."""
        for _ in range(2):
            for ip in ips or self.ips:
                try:
                    self._send(ip, cmd, data)
                except OSError as e:
                    lg.warning("light %s %s failed: %r", ip, cmd, e)
            await asyncio.sleep(GAP)

    async def _turn(self, on: bool) -> None:
        """On or off, then check each lamp and resend to any that didn't switch."""
        await self._all("turn", {"value": int(on)})
        for _ in range(3):
            await asyncio.sleep(0.5)
            missed = []
            for ip in self.ips:
                try:
                    if (await asyncio.to_thread(self._status, ip)).get("onOff") != int(on):
                        missed.append(ip)
                except (OSError, ValueError, KeyError):
                    missed.append(ip)
            if not missed:
                return
            lg.warning("lights %s didn't turn %s; resending", missed, "on" if on else "off")
            await self._all("turn", {"value": int(on)}, missed)

    def _status(self, ip: str) -> dict:
        """The light's state. Govee answers on port 4002, whichever port asked."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as r:
            r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            r.bind(("", REPLY_PORT))
            r.settimeout(1)
            self._send(ip, "devStatus", {})
            return json.loads(r.recvfrom(2048)[0])["msg"]["data"]

    async def _colour(self, rgb=None, kelvin: int = 0) -> None:
        r, g, b = rgb or (0, 0, 0)
        await self._all("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": kelvin})

    async def apply(self, c: Command) -> str:
        """Carry out a command. Returns what the operator says: the command's line, or why it couldn't."""
        if not await self.find():
            return "Sorry caller, I can't find any lights. Is LAN Control on in the Govee app?"
        if self.party:
            self.party.cancel()
            self.party = None
        if c.kind == "off":
            await self._turn(False)
            return c.said
        await self._turn(True)
        if c.kind == "colour":
            await self._colour(c.value)
        elif c.kind == "kelvin":
            await self._colour(kelvin=c.value)
        elif c.kind == "brightness":
            await self._all("brightness", {"value": c.value})
        elif c.kind in ("brighter", "dimmer"):
            try:
                now = (await asyncio.to_thread(self._status, self.ips[0]))["brightness"]
            except (OSError, ValueError, KeyError):
                now = 50
            step = 25 if c.kind == "brighter" else -25
            await self._all("brightness", {"value": max(1, min(100, now + step))})
        elif c.kind == "mode":
            await self._mode(c.value)
        elif c.kind == "party":
            self.party = asyncio.create_task(self._party())
        lg.info("lights %s %s", c.kind, c.value if c.value is not None else "")
        return c.said

    async def snapshot(self) -> dict[str, dict]:
        out = {}
        for ip in self.ips:
            try:
                out[ip] = await asyncio.to_thread(self._status, ip)
            except (OSError, ValueError, KeyError):
                pass
        return out

    async def restore(self, snap: dict[str, dict]) -> None:
        """Put each lamp back as snapshot() found it."""
        for ip, st in snap.items():
            if not st.get("onOff"):
                await self._all("turn", {"value": 0}, [ip])
                continue
            await self._all("brightness", {"value": st.get("brightness", 20)}, [ip])
            c = st.get("color") or {}
            await self._all("colorwc", {"color": {k: c.get(k, 0) for k in "rgb"},
                                        "colorTemInKelvin": st.get("colorTemInKelvin", 0)}, [ip])

    def effect(self, kind: str, until: asyncio.Future | None = None, seconds: float = 20) -> None:
        """Start an effect in the background: "pulse" (Claude orange, breathing) or "party". It runs until
        `until` is done or `seconds` pass, then the lights go back to how they were. A command stops it."""
        if self.party:
            return  # one at a time; the first one's snapshot is the real state
        self.party = asyncio.create_task(self._effect(kind, until, seconds))

    async def _effect(self, kind: str, until: asyncio.Future | None, seconds: float) -> None:
        if not await self.find():
            self.party = None
            return
        snap = await self.snapshot()
        try:
            await self._all("turn", {"value": 1})
            if kind == "pulse":
                await self._colour(RING_COLOUR)
            loop = asyncio.get_running_loop()
            end, n = loop.time() + seconds, 0
            while loop.time() < end and not (until and until.done()):
                n += 1
                for ip in self.ips:
                    try:
                        if kind == "pulse":
                            self._send(ip, "brightness", {"value": 100 if n % 2 else 15})
                        else:
                            r, g, b = random.choice(list(COLOURS.values()))
                            self._send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
                    except OSError:
                        pass
                await asyncio.sleep(0.8 if kind == "pulse" else 0.6)
        except asyncio.CancelledError:
            return  # a command took over: leave the lights to it
        self.party = None
        await asyncio.sleep(GAP)
        await self.restore(snap)

    async def _mode(self, name: str) -> None:
        level, colour, _ = MODES[name]
        await self._all("brightness", {"value": level})
        if isinstance(colour, tuple):
            await self._colour(colour)
        else:
            await self._colour(kelvin=colour)

    async def _party(self, minutes: int = 10) -> None:
        """Each light a different colour, changing about every half second, until another command or `minutes`."""
        await self._all("brightness", {"value": 100})
        palette = list(COLOURS.values())
        loop = asyncio.get_running_loop()
        end = loop.time() + minutes * 60
        while loop.time() < end:
            picks = random.sample(palette, len(self.ips)) if len(self.ips) <= len(palette) else palette
            for ip, (r, g, b) in zip(self.ips, picks):
                try:
                    self._send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
                except OSError:
                    pass
            await asyncio.sleep(0.6)
        self.party = None
        await self._mode("normal")

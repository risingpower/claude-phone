"""Controller daemon: owns every call. Talks to Asterisk over ARI and serves call_<owner> on localhost."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path

from aiohttp import web

from . import config, diary, hold, housekeeping, inject, keychain, lights, log, push, registry, safe, screen, stt, tts
from .ari import ARI, Call, Hangup

lg = log.get("daemon")

MAILBOX = "owner@claude"
VOICEMAIL_MESSAGE = ("{owner} didn't answer. A voicemail has been left. Carry on with other work or your best "
                     "assumption; the answer will arrive later as a new message in this session.")
ANSWER_PREFIX = "[Phone answer to your earlier question]"
ALLHANDS_PREFIX = "[All-hands message from {owner}, sent to every session]"
# Claude Code's spinner line while a turn runs, e.g. "· Generating… (1m 51s · ↓ 4.3k tokens)".
SPINNER = re.compile(r"…\s*\(\d+[hms]|esc to interrupt")
HOLD_MAX = 600  # seconds on hold before the message is taken anyway
# Hangup causes that mean "didn't pick up": no answer, busy, DND (480/486/603), unreachable.
NOT_ANSWERED = "not answered"
# Missed-call reasons where a push would only repeat what the owner knows, or disturb a meeting.
IN_MEETING = "in a meeting"
DND = "do not disturb"
PUSH_SKIP = {"hung up before answering", "answered but nothing was said", IN_MEETING, DND}


def recording_path(name: str) -> Path:
    return config.ASTERISK_PREFIX / "var/spool/asterisk/recording" / f"{name}.wav16"


def ringtone(repo: str, overrides: dict) -> int:
    """T31P ringtone 1-8 for a repo: config override, else the one the registry assigned it."""
    if repo in overrides:
        return int(overrides[repo])
    return registry.ringtone(repo)


def phonebook_xml(sessions: list[registry.Session], s: config.Settings) -> str:
    """Yealink remote phonebook: live sessions, then the fixed lines."""
    from xml.sax.saxutils import escape
    entries = [(caller_id(x.label, s.callerid_max), str(x.ext)) for x in sessions]
    entries += [("Voicemail", s.voicemail_ext), ("Echo test", "600")]
    rows = "".join(f"<DirectoryEntry><Name>{escape(n)}</Name><Telephone>{escape(t)}</Telephone></DirectoryEntry>"
                   for n, t in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><YealinkIPPhoneDirectory>{rows}</YealinkIPPhoneDirectory>'


def browser_xml(sessions: list[registry.Session], s: config.Settings) -> str:
    """Yealink XML Browser directory object (a different schema from the remote phonebook).
    Each item dials its extension when selected."""
    from xml.sax.saxutils import escape
    entries = [(f"{x.ext} {caller_id(x.label, s.callerid_max)}", str(x.ext)) for x in sessions]
    entries += [(f"{s.voicemail_ext} Voicemail", s.voicemail_ext)]
    items = "".join(f"<MenuItem><Prompt>{escape(p)}</Prompt><URI>{escape(u)}</URI></MenuItem>" for p, u in entries)
    title = f"Claude ({len(sessions)} live)"
    return (f'<?xml version="1.0" encoding="UTF-8"?><YealinkIPPhoneDirectory Beep="no" Timeout="60" LockIn="no">'
            f"<Title>{escape(title)}</Title>{items}</YealinkIPPhoneDirectory>")


def caller_id(label: str, limit: int) -> str:
    """Fit repo/branch onto the T31P screen: drop branch prefixes (feat/), then trim the branch, then the repo."""
    if len(label) <= limit:
        return label
    repo, _, branch = label.partition("/")
    branch = branch.rsplit("/", 1)[-1]
    label = f"{repo}/{branch}" if branch else repo
    if len(label) <= limit:
        return label
    if branch:
        room = limit - len(repo) - 1
        if room >= 4:
            return f"{repo}/{branch[:room - 1]}~"
        repo = repo[: max(4, limit // 2 - 1)]
        return f"{repo}/{branch}"[: limit - 1] + "~"
    return label[: limit - 1] + "~"


class Controller:
    def __init__(self):
        self.s = config.load()
        self.ari = ARI(self.s.ari_url, "phone", keychain.get_or_create("ari"))
        self.line = asyncio.Lock()  # one call with the owner at a time
        self.calls: dict[str, asyncio.Task] = {}
        self.endpoint_state: dict[str, str] = {}
        self.safe_play: dict | None = None  # the Crack the Safe call in progress: call, voice
        self.meeting: diary.Meeting | None = None  # the meeting in (or about to start) that holds calls
        self.window: set[str] = set()  # keys of the meetings in the current window
        self.released: set[str] = set()  # meetings whose hold was ended early with the DND key
        self.phone_dnd = False  # the phone's DND key, as reported by its Action URLs
        self.dnd_ours = False  # we switched the phone's DND on for a meeting, so we switch it off after
        self.load_dnd()
        self.lights = lights.Lights(self.s)

    # --- lifecycle ---

    async def start(self) -> None:
        await self.ari.start()
        self.ari.on_event(self.on_event)
        self.ari.on_connect.append(self.subscribe_phones)
        asyncio.create_task(self.ari.run_events())
        await asyncio.to_thread(tts.prerender)
        asyncio.create_task(self.sweeper())
        await asyncio.wait_for(self.ari.connected.wait(), 30)
        await self.update_mwi()
        if self.s.meeting_alerts:
            asyncio.create_task(self.meeting_watch())

    async def meeting_watch(self) -> None:
        """Every 15 s: whisper and show each meeting warn_minutes before it starts, and hold Claude's calls
        from then until it ends. No whisper while another meeting is under way."""
        if not await asyncio.to_thread(diary.access):
            lg.warning("meeting alerts off: no calendar access (System Settings → Privacy & Security → Calendars)")
            return
        found = await asyncio.to_thread(diary.calendars)
        lg.info("meeting alerts on: %s", ", ".join(c for c in found if not self.s.meeting_calendars
                                                   or c in self.s.meeting_calendars) or "no matching calendars")
        alerted: set[str] = set()
        while True:
            try:
                await self.check_meetings(datetime.now().astimezone(), alerted)
            except Exception as e:
                lg.exception("meeting check failed: %r", e)
            await asyncio.sleep(15)

    async def check_meetings(self, now: datetime, alerted: set[str]) -> None:
        warn = timedelta(minutes=self.s.meeting_warn_minutes)
        ms = await asyncio.to_thread(diary.window, now, warn, self.s.meeting_calendars or None)
        held = max(ms, key=lambda m: m.end) if ms else None
        if bool(held) != bool(self.meeting):
            lg.info("meeting window %s", f"until {held.end:%H:%M} ({held.title})" if held else "over")
            if held:  # the lamps are in shot: normal white for the meeting, and no effects until it's over
                asyncio.create_task(self.lights.apply(lights.Command("mode", "normal")))
        self.meeting, self.window = held, {m.key for m in ms}
        if not held and self.dnd_ours:
            self.dnd_ours = False
            self.save_dnd()
            if self.phone_dnd:
                await screen.press("DNDOff")
        for m in ms:
            if m.key in alerted:
                continue
            alerted.add(m.key)
            if m.start < now - timedelta(minutes=1):
                continue  # already under way when first seen (daemon restart): hold calls, no alert
            await self.alert(m, now, whisper=not any(o.start <= now < o.end for o in ms if o is not m))

    async def alert(self, m: diary.Meeting, now: datetime, whisper: bool = True, dnd: bool = True) -> None:
        """Whisper first: the phone rejects even an intercom call once DND is on."""
        title, text = diary.summary(m)
        if whisper:
            await self.whisper(self.s.whisper_text, title)
        if dnd and not self.phone_dnd and await screen.press("DNDOn"):
            self.dnd_ours = True
            self.save_dnd()
        await screen.show(title, text, timeout=int((m.start - now).total_seconds()) + 300)

    def save_dnd(self) -> None:
        """DND state survives a daemon restart, so a meeting released with the DND key stays released.
        Releases are dropped a day after their meeting started (keys end in the start time), unless the
        meeting is still in the current window."""
        cutoff = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
        self.released = {k for k in self.released if k in self.window or k.rpartition("@")[2] >= cutoff}
        state = {"phone_dnd": self.phone_dnd, "dnd_ours": self.dnd_ours, "released": sorted(self.released)}
        try:
            (config.HOME / "dnd.json").write_text(json.dumps(state))
        except OSError as e:
            lg.warning("couldn't save DND state: %r", e)

    def load_dnd(self) -> None:
        try:
            state = json.loads((config.HOME / "dnd.json").read_text())
        except (OSError, ValueError):
            return
        self.phone_dnd, self.dnd_ours = bool(state.get("phone_dnd")), bool(state.get("dnd_ours"))
        self.released = set(state.get("released") or [])

    def light_effect(self, kind: str, **kw) -> None:
        """Lights effects stay out of meetings, as the lamps are behind the owner on camera, unless the DND key
        ended the meeting's hold."""
        if self.meeting and not self.window <= self.released:
            return
        try:
            self.lights.effect(kind, **kw)
        except Exception as e:
            lg.warning("lights effect %s failed: %r", kind, e)

    def hold_reason(self) -> str | None:
        """Why Claude's calls are held right now, if they are: the phone's DND key, or a meeting whose
        hold wasn't ended early by switching DND off."""
        if self.phone_dnd:
            return IN_MEETING if self.meeting else DND
        if self.meeting and not self.window <= self.released:
            return IN_MEETING
        return None

    def set_phone_dnd(self, on: bool) -> None:
        self.phone_dnd = on
        if not on:
            self.dnd_ours = False
            self.released |= self.window  # DND off during a meeting ends its hold
        self.save_dnd()
        lg.info("phone DND %s; calls %s", "on" if on else "off", f"held ({self.hold_reason()})" if self.hold_reason() else "not held")

    async def sweeper(self) -> None:
        missing = sweeps = 0
        while True:
            # Only after two sweeps in a row: on wake from sleep the sweep can beat Wi-Fi reconnecting.
            warning = config.ip_warning()
            missing = missing + 1 if warning else 0
            if missing == 2:
                lg.warning(warning)
            try:
                live = await asyncio.to_thread(inject.live_iterm_ids)
                for s in registry.remove_missing(live):
                    lg.info("removed stale session ext=%s %s", s.ext, s.label)
            except Exception as e:
                lg.warning("stale sweep failed: %r", e)
            if sweeps % 60 == 0:
                try:
                    done = await asyncio.to_thread(housekeeping.run)
                    if any(done.values()):
                        lg.info("housekeeping: %s", done)
                except Exception as e:
                    lg.warning("housekeeping failed: %r", e)
            sweeps += 1
            await asyncio.sleep(60)

    async def subscribe_phones(self) -> None:
        """Watch the phones' registration. Asterisk only sends MWI when the count changes, so a phone
        that was offline when a voicemail arrived never lights up unless we resend on its return."""
        for ext in {self.s.desk_ext, self.s.softphone_ext}:
            try:
                await self.ari.post("/applications/claude-phone/subscription", eventSource=f"endpoint:PJSIP/{ext}")
            except Exception as e:
                lg.warning("endpoint subscription for %s failed: %r", ext, e)

    async def update_mwi(self) -> None:
        n = len(registry.pending_voicemails())
        try:
            await self.ari.put(f"/mailboxes/{MAILBOX}", oldMessages=0, newMessages=n)
        except Exception as e:
            lg.warning("MWI update failed: %r", e)

    async def desk_online(self) -> bool:
        ep = await self.ari.get(f"/endpoints/PJSIP/{self.s.ring_ext}", ok404=True)
        return bool(ep) and ep.get("state") == "online"

    # --- events ---

    def on_event(self, ev: dict) -> None:
        if ev.get("type") == "EndpointStateChange":
            # Also fires when a call starts or ends; only a return from offline needs the light resent.
            ep = ev.get("endpoint", {})
            res, state = ep.get("resource"), ep.get("state")
            was, self.endpoint_state[res] = self.endpoint_state.get(res), state
            if state == "online" and was != "online":
                lg.info("phone %s online; resending message light", res)
                asyncio.create_task(self.update_mwi())
            return
        if ev.get("type") != "StasisStart":
            return
        args = ev.get("args") or []
        if args and args[0] == "outbound":
            return  # handled by the call_owner flow waiting on it
        ch = ev["channel"]
        exten = args[0] if args else ch.get("dialplan", {}).get("exten", "")
        self.calls[ch["id"]] = asyncio.create_task(self.inbound(ch, exten))

    async def inbound(self, ch: dict, exten: str) -> None:
        call = Call(self.ari, ch["id"])
        who = ch.get("caller", {}).get("number")
        lg.info("inbound call from %s to %s", who, exten)
        try:
            if self.line.locked():
                await call.answer()
                await call.play(tts.phrase("busy"))
                return
            async with self.line:
                await call.answer()
                if exten == self.s.voicemail_ext:
                    await self.voicemail(call)
                elif exten == self.s.operator_ext:
                    await self.operator(call)
                elif exten == self.s.allhands_ext:
                    await self.all_hands(call)
                elif exten == self.s.safe_ext:
                    await self.crack_the_safe(call)
                elif exten.isdigit() and self.s.first_session_ext <= int(exten) <= self.s.last_session_ext:
                    await self.dial_session(call, int(exten))
                else:
                    await call.play(tts.phrase("no_session"))
        except Hangup:
            pass
        except Exception as e:
            lg.exception("inbound call failed: %r", e)
        finally:
            try:
                await call.hangup()
            except Exception:
                pass
            call.close()
            self.calls.pop(ch["id"], None)

    # --- me ringing a session ---

    async def operator(self, call: Call) -> None:
        """Dial 0: say who you want and the operator puts you through to that session, or ask for the lights
        ("lights off", "red", "party mode"). After a lights request she listens for another."""
        from . import operator
        sessions = registry.all_sessions()
        await call.play(tts.op(tts.OPERATOR["greeting"]), tts.beep())
        candidates, misses, done = sessions, 0, 0
        while misses < 2 and done < 6:
            name = f"op-{int(time.time())}-{misses}-{done}"
            # Stops after a 2 s pause: nobody hangs up on the operator.
            await call.record(name, 10, max_silence=2)
            heard = await self.transcribe(name, lights.VOCABULARY) or ""
            if done and (not heard or lights.finished(heard)):
                await call.play(tts.op(tts.OPERATOR["bye"]))
                return
            # A request that names the lights is about the lights: "turn the lights back on" isn't the backend.
            # Naming the lights makes it a lights request; otherwise a session first. After one lights command,
            # "turn them back on" is about the lights too.
            command = lights.parse(heard)
            if command and lights.mentions_lights(heard):
                hit = None
            else:
                hit, candidates = operator.match(heard, candidates) if candidates else (None, [])
                command = None if hit else lights.parse(f"lights {heard}" if done else heard)
            lg.info("operator heard %r -> %s%s (candidates %s)", heard, hit and hit.ext,
                    f" lights {command.kind}" if command else "", [c.ext for c in candidates])
            if hit:
                said = await tts.op_async(tts.OPERATOR["connecting"].format(label=tts.spoken_label(hit.label)))
                await call.play(said)
                await self.dial_session(call, hit.ext)
                return
            if command:
                said = await self.lights.apply(command)
                await call.play(await tts.op_async(f"{said} {tts.OPERATOR['else']}"), tts.beep())
                candidates, misses, done = sessions, 0, done + 1
                continue
            misses += 1
            if misses == 2:
                break
            if not candidates:
                candidates = sessions
                await call.play(tts.op(tts.OPERATOR["again"]), tts.beep())
                continue
            options = " or ".join(f"{tts.spoken_label(c.label)}, extension {c.ext}" for c in candidates[:3])
            await call.play(await tts.op_async(tts.OPERATOR["which"].format(options=options)), tts.beep())
        await call.play(tts.op(tts.OPERATOR["unplaced" if sessions else "nobody"]))

    async def dial_session(self, call: Call, ext: int, prefix: str = "", source: str = "") -> str | None:
        """Greet, record, transcribe, type into the session. Returns what was sent (None if nothing)."""
        source = source or f"dial-in {ext}"
        sess = registry.by_ext(ext)
        if not sess:
            lg.info("dialled %s: no session", ext)
            await call.play(tts.phrase("no_session"))
            return
        await self.hold(call, sess)
        greeting = await tts.render_async(tts.PHRASES["connected"].format(label=tts.spoken_label(sess.label)), sess.repo)
        await call.play(greeting, tts.beep())
        name = f"in-{ext}-{int(time.time())}"
        ended, d = await call.record(name, self.s.record_seconds)
        lg.info("dial-in %s recording ended: %s %s", ext, ended, d or "")
        text = await self.transcribe(name)
        if text is None:
            if call.up:
                await call.play(tts.phrase("transcribe_failed"))
            return
        if not text:
            lg.info("dial-in %s: nothing said, nothing sent", ext)
            if call.up:
                await call.play(tts.phrase("nothing_heard"))
            return
        sess = registry.by_ext(ext) or sess  # may have been re-registered during the call
        result = await asyncio.to_thread(inject.type_into, sess, f"{prefix} {text}" if prefix else text, source)
        if call.up:
            await call.play(tts.phrase("sent" if result == "sent" else "inject_failed"))
        return text if result == "sent" else None

    async def mid_task(self, ext: int) -> bool:
        """The hooks say it's working and Claude Code's spinner is on screen. The hooks alone can stick on
        'working': an Esc interrupt fires no Stop."""
        sess = registry.by_ext(ext)
        if not sess or sess.status != "working" or not sess.iterm_id:
            return False
        try:
            screen = await asyncio.to_thread(inject.read_screen, sess.iterm_id)
        except Exception:
            return False
        tail = "\n".join([l for l in screen.splitlines() if l.strip()][-15:])
        return bool(SPINNER.search(tail))

    async def hold(self, call: Call, sess: registry.Session) -> None:
        """If the session is mid-task, play hold music until its turn ends. Hash skips the wait; the
        message then queues in Claude Code behind the running turn."""
        if not await self.mid_task(sess.ext):
            return
        lg.info("dial-in %s: session mid-task, holding", sess.ext)
        free = asyncio.get_running_loop().create_future()

        async def watch() -> None:
            while True:
                await asyncio.sleep(1)
                if not await self.mid_task(sess.ext):
                    free.set_result(True)
                    return

        watcher = asyncio.create_task(watch())
        started = time.monotonic()
        try:
            intro = await tts.op_async(tts.OPERATOR["hold"].format(label=tts.spoken_label(sess.label)))
            got = await call.play(intro, interrupt="#", until=free)
            music, important = await asyncio.to_thread(hold.render), tts.op(tts.OPERATOR["important"])
            while got is None and time.monotonic() - started < HOLD_MAX:
                got = await call.play(music, important, interrupt="#", until=free)
        finally:
            watcher.cancel()
        lg.info("dial-in %s: off hold after %ds (%s)", sess.ext, time.monotonic() - started,
                {"until": "turn ended", "#": "skipped"}.get(got, "timed out"))

    async def crack_the_safe(self, call: Call) -> None:
        """Dial 7233. Guesses are typed as keypad tones on the call: 4 digits then #, * deletes. The board is
        pushed only after each guess: a pushed screen takes over the keypad, and redrawing it on every key
        swallowed the digits (tested 2026-10-16). The phone's own entry screen doesn't show during a call."""
        async def prerender() -> None:  # one at a time: parallel ElevenLabs requests get rate-limited
            for x in safe.STATIC:
                await tts.render_async(x)
        asyncio.create_task(prerender())  # cached after the first game
        self.safe_play = {"call": call, "voice": None}
        try:
            while True:
                demo = safe.take_next_code()
                game = safe.Game(demo)
                lg.info("safe: new game%s", " (demo code)" if demo else "")
                self.safe_say(safe.INTRO)
                cur = ""
                while not game.over:
                    d = await call.next_digit(300)
                    lg.info("safe: key %r", d)
                    if d is None:
                        return
                    if d == "*":
                        cur = cur[:-1]
                    elif d.isdigit():
                        cur = (cur + d)[-safe.DIGITS:]
                    elif d == "#":
                        line = game.guess(cur)
                        cur = ""
                        lg.info("safe: tries %s", game.tries)
                        self.safe_say(line)
                        if not game.over:
                            await screen.push(self.safe_board(game), "safe board")
                if game.won:
                    self.light_effect("party", seconds=20)
                    new_best = not demo and safe.record(len(game.tries))  # a known code doesn't count
                    lg.info("safe: cracked in %s tries%s", len(game.tries), " (best)" if new_best else "")
                else:
                    lg.info("safe: locked out")
                await screen.push(self.safe_board(game), "safe board")
                while (d := await call.next_digit(120)) not in ("#", None):
                    pass
                if d is None:
                    return
        finally:
            lg.info("safe: call ended")
            self.safe_play = None

    def safe_say(self, text: str | None) -> None:
        """The guard's line on the game's call, after any line still playing."""
        play = self.safe_play
        if not play or not text:
            return
        call, prev = play["call"], play["voice"]

        async def go() -> None:
            if prev:
                await asyncio.gather(prev, return_exceptions=True)
            try:
                await call.play(await tts.render_async(text))
            except Hangup:
                pass
        play["voice"] = asyncio.create_task(go())

    def safe_board(self, game: safe.Game) -> str:
        """Stays up until Exit, so earlier guesses can be studied; while it's up the keypad sends no tones."""
        header, lines = game.board(safe.best())
        if game.over:
            lines = ["# to play again", *lines]
        return screen.formatted_screen(header, lines, timeout=600, keys=[("Exit", "SoftKey:Exit")])

    async def all_hands(self, call: Call) -> None:
        """Dial 9: one message typed into every live session."""
        sessions = registry.all_sessions()
        n = len(sessions)
        if not n:
            await call.play(tts.phrase("allhands_none"))
            return
        intro = await tts.render_async(tts.PHRASES["allhands_intro"].format(n=n, sessions="session" if n == 1 else "sessions"))
        await call.play(intro, tts.beep())
        name = f"all-{int(time.time())}"
        ended, d = await call.record(name, self.s.record_seconds)
        lg.info("all-hands recording ended: %s %s", ended, d or "")
        text = await self.transcribe(name)
        if not text:
            lg.info("all-hands: nothing said, nothing sent")
            if call.up:
                await call.play(tts.phrase("nothing_heard" if text == "" else "transcribe_failed"))
            return
        sent = 0
        for sess in registry.all_sessions():  # re-read: sessions may have come or gone during the call
            result = await asyncio.to_thread(inject.type_into, sess, f"{ALLHANDS_PREFIX.format(owner=self.s.owner)} {text}", "all-hands")
            sent += result == "sent"
        lg.info("all-hands sent to %s sessions", sent)
        if call.up:
            await call.play(await tts.render_async(
                tts.PHRASES["allhands_sent"].format(n=sent, sessions="session" if sent == 1 else "sessions")))

    async def transcribe(self, name: str, extra: str = "") -> str | None:
        """Transcript ('' if silent), or None on failure."""
        path = recording_path(name)
        for _ in range(20):  # the file is finalised just after hangup
            if path.exists():
                break
            await asyncio.sleep(0.1)
        if not path.exists():
            lg.warning("recording %s missing", path)
            return ""
        try:
            text = await stt.transcribe(path, extra)
            lg.info("transcript %s: %r", name, text)
            return text
        except Exception as e:
            lg.exception("transcription failed for %s: %r", name, e)
            return None

    # --- Claude ringing me ---

    def caller(self, body: dict) -> dict:
        """Who is calling: the registered session, registering it if its tab predates the hooks."""
        agent = (body.get("from_agent") or "").strip() or None
        sess = registry.find(body.get("session_id"), body.get("iterm_id"))
        if not sess and body.get("session_id") and body.get("iterm_id"):
            from .hooks import repo_info
            cwd = body.get("cwd") or os.getcwd()
            try:
                sess = registry.register(body["session_id"], *repo_info(cwd), cwd, body["iterm_id"])
                lg.info("registered unknown caller ext=%s %s", sess.ext, sess.label)
            except Exception as e:
                lg.warning("could not register caller: %r", e)
        if sess:
            label, ext, sid, repo, branch = sess.label, sess.ext, sess.session_id, sess.repo, sess.worktree or sess.branch
        else:
            from .hooks import repo_info
            repo, b, w = repo_info(body.get("cwd") or os.getcwd())
            branch = w or b
            label, ext, sid = (f"{repo}/{branch}" if branch else repo), None, body.get("session_id") or ""
        # Spoken intro: just the repo, plus the branch only when it isn't the default one.
        intro = tts.spoken_label(f"{repo}/{branch}" if branch and branch not in ("main", "master") else repo)
        if agent:
            label, intro = f"{label}@{agent}", f"{intro}, {agent}"
        return {"label": label, "ext": ext, "sid": sid, "repo": repo, "intro": intro}

    async def ring(self, c: dict, prefix: str, auto_answer: bool = False) -> tuple[Call | None, str]:
        """Ring the desk phone. Returns (answered call, '') or (None, why not). Caller holds self.line.
        auto_answer: an intercom call, which the phone picks up on the speaker without ringing."""
        chan_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
        answered = self.ari.wait_for(lambda e: e.get("type") == "StasisStart" and e["channel"]["id"] == chan_id)
        call = Call(self.ari, chan_id)
        cid = caller_id(c["label"], self.s.callerid_max)
        if auto_answer:
            headers = {"PJSIP_HEADER(add,Alert-Info)": "<http://127.0.0.1>;info=alert-autoanswer",
                       "PJSIP_HEADER(add,Call-Info)": "<sip:127.0.0.1>;answer-after=0"}
        else:
            headers = {"PJSIP_HEADER(add,Alert-Info)": f"<http://127.0.0.1>;info=Ring{ringtone(c['repo'], self.s.repo_ringtones)}"}
        if not auto_answer:
            self.light_effect("pulse", until=answered, seconds=self.s.ring_seconds)
        await self.ari.post("/channels", channelId=chan_id, endpoint=f"PJSIP/{self.s.ring_ext}",
                            app="claude-phone", appArgs="outbound",
                            callerId=f'"{cid}" <{c["ext"] or ""}>', timeout=self.s.ring_seconds,
                            body={"variables": headers})
        done, _ = await asyncio.wait({answered, call.gone}, timeout=self.s.ring_seconds + 5,
                                     return_when=asyncio.FIRST_COMPLETED)
        if answered in done:
            lg.info("call answered for %s", c["label"])
            return call, ""
        self.ari.cancel_wait(answered)
        cause = call.gone.result().get("cause_txt") if call.gone in done else "timeout"
        await call.hangup()
        call.close()
        lg.info("call to %s not answered: %s", self.s.ring_ext, cause)
        return None, f"{NOT_ANSWERED} ({cause})"

    @staticmethod
    def sentence(text: str | None) -> str:
        text = (text or "").strip()
        return text if not text or text[-1] in ".?!" else text + "."

    async def call_owner(self, body: dict) -> dict:
        question = self.sentence(body.get("question"))
        context = self.sentence(body.get("context")) or None
        c = self.caller(body)
        label, ext, sid = c["label"], c["ext"], c["sid"]
        body["_label"] = label
        script = f"{c['intro']}. " + (f"{context} " if context else "") + question
        lg.info("call_owner from %s ext=%s: %r", label, ext, question)

        # Speech renders while the phone rings.
        speech = asyncio.create_task(tts.render_async(script, c["repo"]))

        async def to_voicemail(reason: str) -> dict:
            audio = await speech
            registry.add_voicemail(sid, ext, label, f"{context} {question}" if context else question, context, audio)
            await self.update_mwi()
            lg.info("voicemail left by %s (%s)", label, reason)
            if reason not in PUSH_SKIP:
                push.missed("Missed call", label, question, reason)
            return {"status": "voicemail", "reason": reason, "message": VOICEMAIL_MESSAGE.format(owner=self.s.owner)}

        if not question:
            speech.cancel()
            return {"status": "error", "message": "question is required"}
        if body.get("_force_voicemail"):
            return await to_voicemail(body["_force_voicemail"])
        if held := self.hold_reason():
            return await to_voicemail(held)
        if self.line.locked():
            return await to_voicemail("line busy")
        if not await self.desk_online():
            return await to_voicemail("desk phone offline")

        async with self.line:
            call, why = await self.ring(c, "cj")
            if not call:
                return await to_voicemail(why)
            name = f"cj-{call.id}"
            try:
                await call.play(await speech, tts.beep())
                ended, digit = await call.record(name, self.s.record_seconds)
                lg.info("call_owner recording ended: %s %s", ended, digit or "")
                if call.up and ended == "digit":
                    await call.play(tts.phrase("vm_saved"))
                await call.hangup()
            except Hangup:
                lg.info("%s hung up during the question", self.s.owner)
                return await to_voicemail("hung up before answering")
            finally:
                call.close()
            text = await self.transcribe(name)
            if not text:
                return await to_voicemail("answered but nothing was said" if text == "" else "transcription failed")
            return {"status": "answered", "answer": text}

    async def notify_owner(self, body: dict) -> dict:
        """An announcement: ring, say it, hang up. No reply, and no voicemail if it doesn't land."""
        message = self.sentence(body.get("message"))
        if not message:
            return {"status": "error", "message": "message is required"}
        c = self.caller(body)
        body["_label"] = c["label"]
        lg.info("notify_owner from %s ext=%s: %r", c["label"], c["ext"], message)
        speech = asyncio.create_task(tts.render_async(f"{c['intro']}. {message}", c["repo"]))

        def missed(reason: str) -> dict:
            speech.cancel()
            lg.info("notification from %s not delivered (%s)", c["label"], reason)
            push.missed("Announcement", c["label"], message, reason)
            return {"status": "not_delivered", "reason": reason}

        if held := self.hold_reason():
            speech.cancel()
            lg.info("notification from %s held: %s", c["label"], held)
            return {"status": "on_screen" if self.s.screen_push else "not_delivered", "reason": held}
        if self.line.locked():
            return missed("line busy")
        if not await self.desk_online():
            return missed("desk phone offline")
        async with self.line:
            call, why = await self.ring(c, "nj")
            if not call:
                return missed(why)
            try:
                await call.play(await speech, tts.phrase("goodbye"))
                await call.hangup()
            except Hangup:
                lg.info("%s hung up during the notification", self.s.owner)
                return {"status": "delivered", "note": f"{self.s.owner} hung up before the end"}
            finally:
                call.close()
            return {"status": "delivered"}

    async def whisper(self, text: str, label: str) -> str:
        """Say `text` on the phone's speaker without ringing: an auto-answered intercom call. `label` shows
        as the caller ID. Returns 'delivered' or why not."""
        speech = asyncio.create_task(tts.render_async(text, voice=(self.s.whisper_provider, self.s.whisper_voice)))
        if self.line.locked():
            speech.cancel()
            return "line busy"
        if not await self.desk_online():
            speech.cancel()
            return "desk phone offline"
        async with self.line:
            call, why = await self.ring({"label": label, "repo": None, "ext": ""}, "wh", auto_answer=True)
            if not call:
                speech.cancel()
                return why
            try:
                await call.play(await speech)
                await call.hangup()
            except Hangup:
                pass
            finally:
                call.close()
        lg.info("whispered %r (%s)", text, label)
        return "delivered"

    async def transfer(self, body: dict) -> dict:
        """A session hands the owner to another session: ring, explain, then connect them to the target,
        whose tab receives their words with the handover note."""
        from . import operator
        c = self.caller(body)
        others = [x for x in registry.all_sessions() if x.session_id != c["sid"]]
        target, candidates = operator.match(body.get("to") or "", others)
        if not target:
            listing = [f"{x.ext} {x.label}" for x in (candidates or others)]
            return {"status": "no_match", "message": "No single live session matches that. Pass one of these as `to`.",
                    "sessions": listing}
        reason = self.sentence(body.get("reason"))
        handover = " ".join((body.get("handover") or "").split())
        script = (f"{c['intro']}. " + (f"{reason} " if reason else "")
                  + f"Putting you through to {tts.spoken_label(target.label)}.")
        lg.info("transfer from %s to ext=%s %s: %r", c["label"], target.ext, target.label, reason)
        speech = asyncio.create_task(tts.render_async(script, c["repo"]))

        def missed(why: str) -> dict:
            speech.cancel()
            lg.info("transfer from %s not connected (%s)", c["label"], why)
            return {"status": "not_connected", "reason": why}

        if held := self.hold_reason():
            return missed(held)
        if self.line.locked():
            return missed("line busy")
        if not await self.desk_online():
            return missed("desk phone offline")
        async with self.line:
            call, why = await self.ring(c, "tx")
            if not call:
                return missed(why)
            try:
                await call.play(await speech)
                prefix = f"[Phone transfer from {c['label']}" + (f": {handover}" if handover else "") + f"] {self.s.owner} says:"
                said = await self.dial_session(call, target.ext, prefix=prefix, source=f"transfer {c['ext']}->{target.ext}")
            except Hangup:
                return missed(f"{self.s.owner} hung up before the transfer completed")
            finally:
                await call.hangup()
                call.close()
        if not said:
            return {"status": "connected_no_message", "to": target.label,
                    "message": f"{self.s.owner} was put through to {target.label} but didn't say anything."}
        return {"status": "transferred", "to": target.label, "ext": target.ext, "owner_said": said}

    # --- voicemail ---

    async def voicemail(self, call: Call) -> None:
        items = registry.pending_voicemails()
        if not items:
            await call.play(tts.phrase("vm_none"))
            return
        n = len(items)
        intro = tts.render(tts.PHRASES["vm_intro"].format(n=n, questions="question" if n == 1 else "questions"))
        await call.play(intro, tts.phrase("vm_keys"))
        for vm in items:
            if not call.up:
                break
            frm = await tts.render_async(tts.PHRASES["vm_from"].format(label=tts.spoken_label(vm.label.split("@")[0])))
            media = [frm] + ([vm.audio] if vm.audio else [await tts.render_async(vm.question)]) + [tts.beep()]
            call.drain_digits()
            digit = await call.play(*media, interrupt="#7")
            if digit == "#":
                await call.play(tts.phrase("vm_skipped"))
                continue
            if digit == "7":
                registry.delete_voicemail(vm.id)
                lg.info("voicemail %s deleted (%s)", vm.id, vm.label)
                await self.update_mwi()
                await call.play(tts.phrase("vm_deleted"))
                continue
            name = f"vm-{vm.id}-{int(time.time())}"
            try:
                ended, d = await call.record(name, self.s.record_seconds, terminate_on="any")
            except Hangup:
                ended, d = "hangup", None
            lg.info("voicemail %s recording ended: %s %s", vm.id, ended, d or "")
            if d == "7":
                registry.delete_voicemail(vm.id)
                lg.info("voicemail %s deleted (%s)", vm.id, vm.label)
                await self.update_mwi()
                await call.play(tts.phrase("vm_deleted"))
                continue
            text = await self.transcribe(name)
            if not text:
                if call.up:
                    await call.play(tts.phrase("vm_skipped"))
                continue
            delivered = await self.deliver_answer(vm, text)
            await self.update_mwi()
            if call.up:
                await call.play(tts.phrase("vm_saved" if delivered else "vm_session_gone"))
        if call.up and not registry.pending_voicemails():
            await call.play(tts.phrase("vm_done"))

    async def deliver_answer(self, vm: registry.Voicemail, answer: str) -> bool:
        sess = registry.find(vm.session_id)
        if not sess:
            registry.answer_voicemail(vm.id, answer, "session-ended")
            lg.info("voicemail %s answered but session ended; kept: %r", vm.id, answer)
            return False
        q = vm.question if len(vm.question) <= 160 else vm.question[:157] + "..."
        text = f'{ANSWER_PREFIX} {answer} (Your question was: "{q}")'
        result = await asyncio.to_thread(inject.type_into, sess, text, f"voicemail {vm.id}")
        registry.answer_voicemail(vm.id, answer, "sent" if result == "sent" else f"failed:{result}")
        return result == "sent"

    # --- HTTP (localhost) ---

    async def http_transfer(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            return web.json_response(await self.transfer(body))
        except Exception as e:
            lg.exception("transfer failed: %r", e)
            return web.json_response({"status": "not_connected", "reason": f"error: {e.__class__.__name__}"})

    async def show_on_screen(self, body: dict, said: str, res: dict) -> None:
        """A call or announcement as a summary on the phone's screen, once the line is free."""
        if not self.s.screen_push or not body.get("_label") or res.get("status") == "error":
            return
        text = " ".join((body.get("screen") or said or "").split())
        if len(text) > 80:
            text = text[:77] + "..."
        if res.get("status") == "voicemail":
            text += "\n(voicemail waiting)"
        await screen.show(body["_label"], text, timeout=600)

    async def http_notify(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            res = await self.notify_owner(body)
            asyncio.create_task(self.show_on_screen(body, body.get("message"), res))
            return web.json_response(res)
        except Exception as e:
            lg.exception("notify_owner failed: %r", e)
            return web.json_response({"status": "not_delivered", "reason": f"error: {e.__class__.__name__}"})

    async def http_call(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            res = await self.call_owner(body)
            asyncio.create_task(self.show_on_screen(body, body.get("question"), res))
            return web.json_response(res)
        except Exception as e:
            # Never lose the question: whatever broke, it becomes a voicemail.
            lg.exception("call_owner failed: %r", e)
            try:
                return web.json_response(await self.call_owner({**body, "_force_voicemail": f"error: {e.__class__.__name__}"}))
            except Exception:
                return web.json_response({"status": "error", "message": "The phone system failed. Carry on with your best assumption."})

    async def http_sessions(self, request: web.Request) -> web.Response:
        """Live sessions as a Yealink directory. Served at /phonebook.xml (Directory → Claude; the phone
        caches it for an hour or more) and /sessions.xml (an XML Browser soft key fetches it on every press)."""
        if ip_address(request.remote or "0.0.0.0") not in ip_network(self.s.lan_cidr):
            raise web.HTTPForbidden()
        lg.info("phonebook %s fetched by %s", request.path, request.remote)
        render = browser_xml if request.path == "/sessions.xml" else phonebook_xml
        return web.Response(text=render(registry.all_sessions(), self.s), content_type="text/xml")

    async def http_whisper(self, request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"status": await self.whisper(body["text"], body.get("label") or "Psst")})

    async def http_dnd(self, request: web.Request) -> web.Response:
        """The phone's Action URLs: Open DND -> /dnd/on, Close DND -> /dnd/off."""
        if ip_address(request.remote or "0.0.0.0") not in ip_network(self.s.lan_cidr):
            raise web.HTTPForbidden()
        self.set_phone_dnd(request.match_info["state"] == "on")
        return web.Response(text="ok")

    async def http_meeting_test(self, request: web.Request) -> web.Response:
        """`phone meeting-test`: the alert for a made-up meeting. Calls are not held."""
        now = datetime.now().astimezone()
        start = now + timedelta(minutes=self.s.meeting_warn_minutes)
        await self.alert(diary.Meeting("test", "Test meeting", start, start + timedelta(minutes=30),
                                       ("Sarah Jones", "Dan Smith")), now, dnd=False)
        return web.json_response({"ok": True})

    async def http_lights(self, request: web.Request) -> web.Response:
        """`phone lights <words>`: the same commands the operator takes."""
        text = (await request.json()).get("text") or ""
        if text.split()[-1:] == ["find"]:
            return web.json_response({"lights": await self.lights.find(fresh=True)})
        command = lights.parse(text)
        if not command:
            return web.json_response({"error": f"not a lights command: {text!r}"}, status=400)
        return web.json_response({"said": await self.lights.apply(command), "lights": self.lights.ips})

    async def http_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ip_warning": config.ip_warning(), "ari": self.ari.connected.is_set(),
                                  "desk_online": await self.desk_online(), "line_busy": self.line.locked()})


async def main() -> None:
    config.ensure_dirs()
    c = Controller()
    await c.start()
    app = web.Application()
    app.router.add_post("/call", c.http_call)
    app.router.add_post("/notify", c.http_notify)
    app.router.add_post("/transfer", c.http_transfer)
    app.router.add_post("/whisper", c.http_whisper)
    app.router.add_post("/meeting-test", c.http_meeting_test)
    app.router.add_post("/lights", c.http_lights)
    app.router.add_get("/health", c.http_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", c.s.daemon_port).start()
    book = web.Application()
    book.router.add_get("/phonebook.xml", c.http_sessions)
    book.router.add_get("/sessions.xml", c.http_sessions)
    book.router.add_get("/dnd/{state:on|off}", c.http_dnd)
    book_runner = web.AppRunner(book)
    await book_runner.setup()
    lg.info("daemon up on 127.0.0.1:%s, ringing PJSIP/%s", c.s.daemon_port, c.s.ring_ext)
    asyncio.create_task(serve_phonebook(book_runner, c.s))
    await asyncio.Event().wait()


async def serve_phonebook(runner: web.AppRunner, s: config.Settings) -> None:
    """The phonebook binds to the home LAN address, which is missing while the Mac is on another network.
    Keep retrying rather than taking the daemon (and missed-call pushes) down with it."""
    while True:
        try:
            await web.TCPSite(runner, s.lan_ip, s.phonebook_port).start()
            lg.info("phonebook on %s:%s", s.lan_ip, s.phonebook_port)
            return
        except OSError:
            await asyncio.sleep(60)


def run() -> None:
    asyncio.run(main())

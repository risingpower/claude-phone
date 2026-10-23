"""Call flows against a scripted fake call: no Asterisk, audio, whisper or iTerm."""

import asyncio

import pytest

from claude_phone import daemon, inject, push, registry, tts


class FakeCall:
    def __init__(self, digits=(), recordings=()):
        self.id = "c1"
        self.up = True
        self.played = []
        self.digits = list(digits)  # what each interruptible play returns
        self.recordings = list(recordings)

    async def play(self, *media, interrupt="", until=None):
        self.played.extend(media)
        return self.digits.pop(0) if interrupt and self.digits else None

    async def record(self, name, max_seconds, terminate_on="#", max_silence=None):
        return self.recordings.pop(0) if self.recordings else ("finished", None)

    def drain_digits(self):
        pass

    async def hangup(self):
        self.up = False

    def close(self):
        pass


@pytest.fixture
def ctl(monkeypatch):
    """A Controller with speech, transcription, typing and the message light stubbed out."""
    c = daemon.Controller.__new__(daemon.Controller)
    c.s = daemon.config.load()
    c.line = asyncio.Lock()
    c.meeting, c.window, c.released, c.phone_dnd, c.dnd_ours = None, set(), set(), False, False

    class Lamps:  # records what the lamps were told
        def __init__(self):
            self.done = []

        async def apply(self, command):
            self.done.append((command.kind, command.value))

        def effect(self, kind, **kw):
            self.done.append(("effect", kind))
    c.lights = Lamps()

    async def no_key(key):
        return False
    monkeypatch.setattr(daemon.screen, "press", no_key)
    c.typed, c.heard, c.pushed = [], [], []

    async def render_async(text, repo=None, voice=None):
        return f"tts:{text}"
    monkeypatch.setattr(tts, "render", lambda text, repo=None, voice=None: f"tts:{text}")
    monkeypatch.setattr(tts, "render_async", render_async)
    monkeypatch.setattr(tts, "beep", lambda: "beep")

    async def transcribe(name):
        return c.heard.pop(0) if c.heard else ""
    monkeypatch.setattr(c, "transcribe", transcribe)

    async def nothing(*a, **k):
        return False
    monkeypatch.setattr(c, "update_mwi", nothing)
    monkeypatch.setattr(c, "mid_task", nothing)

    def type_into(sess, text, source):
        c.typed.append((sess.ext, text))
        return "sent"
    monkeypatch.setattr(inject, "type_into", type_into)
    monkeypatch.setattr(push, "missed", lambda *a: c.pushed.append(a))
    return c


def run(coro):
    return asyncio.run(coro)


def test_dial_in_types_what_was_said(ctl):
    registry.register("s", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["What colour should the button be?"]
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) == "What colour should the button be?"
    assert ctl.typed == [(301, "What colour should the button be?")]
    assert "tts:Sent." in call.played


def test_dial_in_silence_sends_nothing(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    call = FakeCall()
    assert run(ctl.dial_session(call, 301)) is None
    assert ctl.typed == []
    assert "tts:" + tts.PHRASES["nothing_heard"] in call.played


def test_dial_in_empty_extension(ctl):
    call = FakeCall()
    run(ctl.dial_session(call, 399))
    assert call.played == ["tts:" + tts.PHRASES["no_session"]]


def test_transfer_prefix_reaches_target(ctl):
    registry.register("t", "site-be-v2", "main", None, "/r", "tab")
    ctl.heard = ["Testing."]
    run(ctl.dial_session(FakeCall(), 301, prefix="[Phone transfer from app-fe-v2] Sam says:"))
    assert ctl.typed == [(301, "[Phone transfer from app-fe-v2] Sam says: Testing.")]


def test_voicemail_answer_skip_delete(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    registry.add_voicemail("s", 301, "repo/main", "Merge it?", None, None)
    b = registry.add_voicemail("s", 301, "repo/main", "Which colour?", None, None)
    registry.add_voicemail("s", 301, "repo/main", "Delete me", None, None)
    ctl.heard = ["Yes, merge it."]
    call = FakeCall(digits=[None, "#", "7"])  # answer the first, skip the second, delete the third
    run(ctl.voicemail(call))
    assert ctl.typed == [(301, '[Phone answer to your earlier question] Yes, merge it. (Your question was: "Merge it?")')]
    assert [v.id for v in registry.pending_voicemails()] == [b]


def test_answer_for_closed_session_is_kept(ctl):
    vm = registry.add_voicemail("gone", None, "repo/main", "Ship?", None, None)
    ctl.heard = ["Yes."]
    call = FakeCall()
    run(ctl.voicemail(call))
    assert ctl.typed == [] and registry.pending_voicemails() == []
    assert "tts:" + tts.PHRASES["vm_session_gone"] in call.played


def test_unanswered_call_leaves_voicemail_and_pushes(ctl, monkeypatch):
    registry.register("s", "repo", "main", None, "/r", "tab")

    async def online():
        return True

    async def ring(c, prefix):
        return None, "not answered"
    monkeypatch.setattr(ctl, "desk_online", online)
    monkeypatch.setattr(ctl, "ring", ring)
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "iterm_id": "tab"}))
    assert res["status"] == "voicemail"
    assert [v.question for v in registry.pending_voicemails()] == ["Merge it."]
    assert ctl.pushed == [("Missed call", "repo/main", "Merge it.", "not answered")]


def test_hanging_up_on_a_question_does_not_push(ctl):
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "_force_voicemail": "hung up before answering"}))
    assert res["status"] == "voicemail" and ctl.pushed == []


def test_voicemail_message_names_the_owner(ctl):
    ctl.s.owner_name = "Sam"
    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s", "_force_voicemail": "line busy"}))
    assert res["message"].startswith("Sam didn't answer.")


def meeting(start_min, length_min=30, title="Board review", people=("Sarah Jones", "Dan Smith")):
    from datetime import datetime, timedelta
    from claude_phone import diary
    now = datetime(2026, 10, 14, 14, 58).astimezone()
    start = now + timedelta(minutes=start_min)
    return now, diary.Meeting(f"e1@{start}", title, start, start + timedelta(minutes=length_min), people)


def test_meeting_whispers_once_shows_screen_and_holds_calls(ctl, monkeypatch):
    from claude_phone import diary, screen
    now, m = meeting(2)
    monkeypatch.setattr(diary, "window", lambda *a: [m])
    shown, whispered = [], []

    async def show(title, text, timeout=120):
        shown.append((title, text))

    async def whisper(text, label):
        whispered.append(label)
    monkeypatch.setattr(screen, "show", show)
    monkeypatch.setattr(ctl, "whisper", whisper)
    alerted = set()
    run(ctl.check_meetings(now, alerted))
    run(ctl.check_meetings(now, alerted))
    assert whispered == [f"{m.start:%H:%M} Board review"]
    assert shown == [(f"{m.start:%H:%M} Board review", f"with Sarah, Dan\nDND until {m.end:%H:%M}")]
    assert ctl.meeting == m
    assert ctl.lights.done == [("mode", "normal")]  # once, at the start of the window
    ctl.light_effect("pulse")
    assert ctl.lights.done == [("mode", "normal")]  # no effects in a meeting
    ctl.set_phone_dnd(False)  # the DND key ends the meeting's hold, and lets effects back in
    ctl.light_effect("pulse")
    assert ctl.lights.done[-1] == ("effect", "pulse")
    ctl.released.clear()

    registry.register("s", "repo", "main", None, "/r", "tab")
    res = run(ctl.call_owner({"question": "Merge it", "session_id": "s"}))
    assert res["reason"] == "in a meeting" and ctl.pushed == []
    ctl.s.screen_push = True
    assert run(ctl.notify_owner({"message": "Build done", "session_id": "s"}))["status"] == "on_screen"


def test_meeting_already_under_way_holds_calls_without_alert(ctl, monkeypatch):
    from claude_phone import diary
    now, m = meeting(-10)
    monkeypatch.setattr(diary, "window", lambda *a: [m])

    async def boom(*a, **k):
        raise AssertionError("no alert expected")
    monkeypatch.setattr(ctl, "whisper", boom)
    run(ctl.check_meetings(now, set()))
    assert ctl.meeting == m


def test_meeting_summary_trims_people():
    from claude_phone import diary
    _, m = meeting(2, people=("Ann Lee", "bob@x.com", "Cy Ng", "Di Fox"))
    assert diary.summary(m)[1].startswith("with Ann, bob +2\n")


def test_call_summary_on_screen_prefers_the_sessions_summary(ctl, monkeypatch):
    from claude_phone import screen
    shown = []

    async def show(title, text, timeout=120):
        shown.append((title, text))
    monkeypatch.setattr(screen, "show", show)
    ctl.s.screen_push = True
    body = {"_label": "repo/main", "screen": "Merge the payments PR?"}
    run(ctl.show_on_screen(body, "Shall I merge the payments pull request now?", {"status": "voicemail"}))
    run(ctl.show_on_screen({"_label": "repo/main"}, "x" * 100, {"status": "delivered"}))
    assert shown == [("repo/main", "Merge the payments PR?\n(voicemail waiting)"), ("repo/main", "x" * 77 + "...")]


def test_dnd_key_off_ends_a_meeting_hold_and_on_holds_calls(ctl, monkeypatch):
    from claude_phone import diary, screen
    now, m = meeting(2)
    monkeypatch.setattr(diary, "window", lambda *a: [m])

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(screen, "show", nothing)
    monkeypatch.setattr(ctl, "whisper", nothing)
    run(ctl.check_meetings(now, set()))
    assert ctl.hold_reason() == "in a meeting"
    ctl.set_phone_dnd(False)  # DND key off: the meeting finished early
    assert ctl.hold_reason() is None
    again = daemon.Controller.__new__(daemon.Controller)  # a daemon restart keeps the release
    again.load_dnd()
    again.meeting, again.window = ctl.meeting, ctl.window
    assert again.released == ctl.released and again.hold_reason() is None
    ctl.set_phone_dnd(True)
    assert ctl.hold_reason() == "in a meeting"
    monkeypatch.setattr(diary, "window", lambda *a: [])
    run(ctl.check_meetings(now, set()))
    assert ctl.hold_reason() == "do not disturb"
    ctl.set_phone_dnd(False)
    assert ctl.hold_reason() is None

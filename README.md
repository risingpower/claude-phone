# claude-phone

A desk phone becomes the line between you and your Claude Code sessions.

**Came from Instagram?** [Watch it in action](https://www.instagram.com/reel/DeY8JPmszXs/), then follow the setup below to build your own.

> **Mac only (Apple Silicon, M1 or later).** It relies on macOS throughout: iTerm2 scripting to type into
> sessions, the `say` voices, the keychain and launchd. There are no Windows, Linux or Intel Mac instructions.

- **Claude rings you:** a session calls the `call_<name>` MCP tool (`call_sam` for `owner_name = "Sam"`). The phone rings with `repo/branch` as caller ID; the spoken answer comes back as the tool result.
- **Claude announces:** `notify_<name>` rings, reads a short announcement and hangs up.
- **Transfers:** `transfer_call` puts you through to another session when you ask the wrong one.
- **You ring Claude:** every live session has an extension (301-399). Dial it, speak, hang up; the words are typed into that session's iTerm2 tab and submitted.
- **Voicemail:** unanswered questions light the message LED. Press Message, answer each one; answers are typed into the asking sessions.

Everything runs locally on one Mac: Asterisk (the phone exchange), whisper.cpp (speech to text), macOS `say`
or ElevenLabs (text to speech) and a small Python daemon. Your voice never leaves the Mac; the only things sent out
are text to ElevenLabs, if you choose it, and missed-call pushes, if you set them up. Why each piece is built the way it is: [docs/build-notes.md](docs/build-notes.md).

## What you need

- An Apple Silicon Mac with [iTerm2](https://iterm2.com) and [Homebrew](https://brew.sh); Claude Code runs in iTerm2 tabs.
- A SIP desk phone on the same network as the Mac, plugged into the router with a network cable.
  Built and tested on a **Yealink T31P**; [docs/t31p-setup.md](docs/t31p-setup.md) walks through it.
  The T31P is powered over the network cable (PoE), and second-hand ones often come without a power supply:
  without a PoE switch or injector, buy the Yealink 5 V power adapter as well.
  Other SIP phones should work but are untested; per-repo ringtones and the live session list are Yealink features.
- No desk phone yet? [Linphone](https://linphone.org) (free, Mac or mobile) works for trying everything out.
- Python 3.12+, Xcode command line tools (to build Asterisk), and about 1 GB of disk for the speech model.
- No accounts or subscriptions. ElevenLabs (nicer voices) and ntfy (missed-call pushes) are optional.

## Dial plan

| Ext | What |
|---|---|
| 301-399 | live Claude Code sessions (`phone list`). Mid-task, you get hold music until its turn ends; # leaves your message now |
| 200 | T31P |
| 201 | Linphone (testing) |
| 0 | operator: say who you want ("the backend on the AGS branch") and it puts you through. Or ask for the lights: "lights off", "red", "fifty percent", "focus", "party mode" (Govee lights with LAN Control on in the Govee app; also `phone lights <words>`). The lights also pulse while Claude rings and party when the safe is cracked, then go back as they were; in meetings they stay on normal white, since they're in shot |
| 9 | all-hands: your message is typed into every live session |
| 7233 | Crack the Safe (S-A-F-E): find the 4-digit code in 8 tries; type 4 digits then #, the board shows after each guess while a guard heckles; Exit closes it to type the next one. Needs `screen_push` |
| 500 | voicemail (T31P Message key) |
| 600 | echo test |
| 601 | 1 kHz tone |

On any recording: `#` ends it, hanging up also ends it. In voicemail: `#` during a question skips it, `7` deletes it.

## Setup

0. **Tools:** `xcode-select --install`, then `brew install openssl@3 whisper-cpp`.
1. **Asterisk** (about 10 minutes): `scripts/build-asterisk.sh` builds 22.11 into `~/.claude-phone/asterisk`.
   The first start after a build takes about 45 s while macOS scans the new libraries.
2. **Speech model:** the first start (step 6) downloads `ggml-large-v3-turbo-q5_0.bin` (~550 MB) on first run.
3. **Python:** `python3 -m venv .venv && .venv/bin/pip install -e .`
4. **Config:** `cp config.example.toml ~/.claude-phone/config.toml` and set `owner_name`. Set `ring_ext = "201"` to test on Linphone.
5. **Secrets** are in the login keychain (service `claude-phone`): `phone secret sip` prints the SIP password for the phones; `ari` is internal. ElevenLabs: `ELEVENLABS_API_KEY` or keychain account `elevenlabs`.
6. **Start:** `.venv/bin/phone install-services` (runs everything now and at every login), then `.venv/bin/phone status`.
7. **Desk phone:** [docs/t31p-setup.md](docs/t31p-setup.md). Dial **600** and you should hear yourself (echo test).
8. **Claude Code wiring** (global; see below).
9. **CLAUDE.md:** add [docs/claude-md-snippet.md](docs/claude-md-snippet.md) to `~/.claude/CLAUDE.md`.
10. **Try it:** open a new Claude Code session in iTerm2 and ask it to ring you. Then dial its extension
    (`phone list`) and speak: the first time, macOS asks to let the daemon control iTerm2. Allow it.

### Claude Code wiring

From the repo folder:

```sh
claude mcp add --scope user claude-phone -- "$PWD/.venv/bin/phone" mcp
```

plus four hooks in `~/.claude/settings.json` (replace `/path/to/claude-phone`):

```json
"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "/path/to/claude-phone/.venv/bin/phone hook session-start"}]}],
  "SessionEnd": [{"hooks": [{"type": "command", "command": "/path/to/claude-phone/.venv/bin/phone hook session-end"}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/claude-phone/.venv/bin/phone hook prompt"}]}],
  "Stop": [{"hooks": [{"type": "command", "command": "/path/to/claude-phone/.venv/bin/phone hook stop"}]}]
}
```

Then start Claude Code sessions in iTerm2 tabs; each one gets an extension (`phone list`).

### Voices

Out of the box every call uses the Mac's built-in **Daniel** voice (British English, macOS `say`): free, offline,
no account. It's the voice in the original demo video. Set `say_voice` in config.toml to change it; `say -v '?'` lists the voices, and better
"Enhanced" and "Premium" ones download in System Settings → Accessibility → Spoken Content → System Voice.

The other demo clips use **Jessica** from the ElevenLabs voice library. To use her, or any ElevenLabs voice:

```toml
tts_provider = "elevenlabs"
elevenlabs_voice_id = "r1KmysJdVYZjJCm4mL3b"   # Jessica
operator_provider = "elevenlabs"               # the operator on 0
operator_voice = "r1KmysJdVYZjJCm4mL3b"
```

and put your API key in the keychain (`security add-generic-password -s claude-phone -a elevenlabs -w`) or
`ELEVENLABS_API_KEY`. Speech is cached, so repeated phrases cost nothing. If ElevenLabs fails or there's no key,
that call falls back to the Mac voice rather than going silent. Each repo can have its own voice (`repo_voices`
in [config.example.toml](config.example.toml)).

### Meetings and the screen (optional)

With `screen_push = true`, every call and announcement also leaves a short summary on the phone's screen
(sessions write it in the tools' `screen` field). With `meeting_alerts = true`, two minutes before each meeting
in the Mac's Calendar app (any synced account: Google, iCloud, Exchange) the phone answers itself on the
speaker and whispers "Pssst, you have a meeting", without ringing, then shows the time, title, who it's with
and when it ends. From then until the meeting ends, Claude's calls are held: questions go to voicemail,
announcements go to the screen only, and nothing is pushed to your mobile.

- **Phone:** the screen needs the T31P to trust the Mac ([docs/t31p-setup.md](docs/t31p-setup.md) step 11).
- **Mac:** the first time, allow calendar access and local network access for Python when macOS asks
  (System Settings → Privacy & Security → Calendars / Local Network).
- **DND key:** the Mac switches the phone's DND on at each alert and off when the meeting ends. Switch it off
  yourself to end the hold early; switch it on any time to hold Claude's calls (step 12 of the phone guide).
- `phone meetings` lists your calendars and the meetings it will use; `meeting_calendars = ["Work"]` limits it.
- `phone meeting-test` plays a made-up alert so you can hear and see it. Calls are not held.
- The whisper is the Mac's Whisper voice by default. Jessica whispering (ElevenLabs v3):
  `whisper_provider = "elevenlabs_v3"`, `whisper_voice = "r1KmysJdVYZjJCm4mL3b"`,
  `whisper_text = "[whispers] Pssst... you have a meeting."`

### Missed-call pushes (ntfy)

Install the free ntfy app, run `phone push` in a terminal and subscribe to the topic it prints (treat it as a password), then `phone push test`. A `call_<name>` that goes to voicemail, or a `notify_<name>` that isn't heard, then pushes to the app. Pushes pass through the public ntfy.sh server; set `push_text = false` in config.toml to send only who called.

## Running

Everything runs as launchd user agents (`com.claude-phone.{asterisk,whisper,daemon}`, KeepAlive, start at login):
`phone install-services` / `phone uninstall-services`. Without them, `phone up` runs the same processes by hand.

```sh
phone restart daemon   # after config changes (asterisk: also regenerates its config)
phone up            # asterisk, whisper-server, daemon (each also: phone up daemon)
phone status        # processes, daemon health, registered phones
phone list          # live sessions and extensions
phone vm            # pending voicemail questions
phone down
tail -f ~/.claude-phone/logs/phone.log   # calls, transcripts, every injection
```

`phone` is `.venv/bin/phone`.

## Files

| Path | What |
|---|---|
| `~/.claude-phone/registry.db` | sessions, voicemails, injection log (SQLite) |
| `~/.claude-phone/logs/phone.log` | the log to tail |
| `~/.claude-phone/logs/{asterisk,whisper,daemon}.out` | process output |
| `~/.claude-phone/logs/asterisk/messages.log` | Asterisk log |
| `~/.claude-phone/etc/asterisk/` | generated Asterisk config (from `asterisk/*.tmpl`, `phone config`) |
| `~/.claude-phone/sounds/` | TTS cache |

## Tests

`.venv/bin/pip install -e '.[dev]'` then `.venv/bin/pytest`. They run against a throwaway home and a fake call: no Asterisk, audio, whisper or iTerm needed.

## Troubleshooting

- **Call connects, no audio:** every endpoint needs `media_address`, `bind_rtp_to_media_address=yes` and `rtp_ipv6=no` (`asterisk/pjsip.conf.tmpl`). If the Mac's IP changes, update `lan_ip` and run `phone config && phone restart asterisk`.
- **Phone won't register:** `phone status` shows contacts. Check the password (`phone secret sip`) and that the phone is on `lan_cidr`. Asterisk log: `~/.claude-phone/logs/asterisk/messages.log`.
- **Asterisk console:** `~/.claude-phone/asterisk/sbin/asterisk -C ~/.claude-phone/etc/asterisk/asterisk.conf -r`
- **`call_<name>` always goes to voicemail:** `phone status` → `desk_online`. Asterisk qualifies the phone every 10 s, so a phone that was just plugged in can take that long to count as online.
- **Text not typed:** the tab must be in `phone list`. iTerm2 needs Automation permission for whatever runs the daemon (System Settings → Privacy & Security → Automation).
- **Wrong words for repo names:** whisper is prompted with live repo and branch names; check `phone list` spelling.

## Credits

Built by [@risingpower](https://www.instagram.com/risingpower/) with Claude Code.

## License

MIT. See [LICENSE](LICENSE).

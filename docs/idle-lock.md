# Idle lock (fork `sawrus/aipager`)

Whoever holds the owner's Telegram account (a lost phone, a desktop
session left open) can drive every Claude session, and through it the
machine. The idle lock bounds that window with a second factor: after a
quiet spell aipager needs a code from an authenticator app.

Set it up once:

```
aipager lock setup            # makes the TOTP secret, prints its otpauth:// URI (and a QR code with qrencode) once
aipager lock enable           # on; --after-min 60|480|960 for 1, 8 or 16 hours (default 60)
aipager lock status|now|disable
```

`aipager lock now` on a locked aipager clears the chats again at the
daemon's next tick and sends the lock notice again.

The secret is in `~/.config/aipager/totp.env` (`0600`), the state in
`~/.local/state/aipager/lock.json` (`0600`), the settings in `aipager.yaml`.
Both folders are kept `0700`, also when an older install made them wider:

```yaml
security:
  idle_lock:
    enabled: true
    after_min: 60
```

**The time** is one of three: 1 hour (`60`, the default), 8 hours (`480`) or
16 hours (`960`). `aipager lock enable --after-min N` takes only those and
refuses any other number. In Telegram, `/settings` -> **🔒 Idle lock** shows
the three times with ✅ on the current one; an owner or admin taps one, also
in a private chat, because the time is for the whole install, not one chat.
The tap writes `after_min` and takes effect at once, with no restart. A lock
that is off keeps the picked time for when it is turned on. A value written
by hand in `aipager.yaml` outside the three stays in effect and shows as
`custom: N min` with no ✅ until one of the three is picked.

Only a real YAML `true` turns it on, and `aipager config` keeps the block.
The daemon reads all three while it runs: no restart is needed.

**What counts as activity:** any message or button tap from a member of a
served chat, and any Mini App request that changes something (a write).
Mini App reads do not count: the page polls by itself, and an app left
open must not keep aipager unlocked.

**When the quiet spell is over** (checked every 30 seconds) aipager:

- deletes the messages it knows of in every served chat (sent and
  received; each message once, edits are not counted again) and, in a
  private chat, every message id since the previous wipe, in calls of
  100 (Telegram allows it for 48 hours). A delete that fails is tried
  again at the next wipe, and the log says per chat how many ids were
  asked and how many failed (never their text),
- sends `🔒 aipager is locked after a quiet spell. Send /start to unlock it.`
  to each private chat,
- refuses every outbound Bot API call that carries a chat, in the rate
  limiter that every call passes (`IdleLocked`, a `FloodMuted`): session
  output, cards and permission prompts are not sent; only deleting
  messages, answering taps and the lock's own lines pass,
- stops every inbound update before any other handler (group -4),
- answers every Mini App route with `423 {"error": "locked"}`.

Sessions keep running. What they sent while locked is not sent again
later; after the unlock, `/status` shows where each session is.

**Unlock:** `/start` in a member's private chat, then the 6-digit code.
A code is never taken in a group (everyone there would see it). The code
message is deleted. A code is valid for its 30-second step and one step
each way, and each step is accepted once. Five wrong codes in ten minutes
pause the checks for the rest of those ten minutes.

**Limits.** The lock is only as strong as the bot token: the Mini App's
`initData` signature is derived from it, so a leaked token must be revoked
in @BotFather. Enabled without a secret, the lock stays off (nobody could
unlock it) and the log says so. On the machine itself, `aipager lock
disable` turns it off: the lock guards the Telegram side, not a local user.

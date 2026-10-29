# Idle lock (fork `sawrus/aipager`)

Whoever holds the owner's Telegram account (a lost phone, a desktop
session left open) can drive every Claude session, and through it the
machine. The idle lock bounds that window with a second factor: after a
quiet spell aipager needs a code from an authenticator app.

Set it up once:

```
aipager lock setup            # makes the TOTP secret, prints its otpauth:// URI (and a QR code with qrencode) once
aipager lock enable           # on; --after-min N to change the 60 minutes
aipager lock status|now|disable
```

The secret is in `~/.config/aipager/totp.env` (`0600`), the state in
`~/.local/state/aipager/lock.json` (`0600`), the settings in `aipager.yaml`:

```yaml
security:
  idle_lock:
    enabled: true
    after_min: 60
```

Only a real YAML `true` turns it on, and `aipager config` keeps the block.
The daemon reads all three while it runs: no restart is needed.

**What counts as activity:** any message or button tap from a member of a
served chat, and any Mini App request that changes something (a write).
Mini App reads do not count: the page polls by itself, and an app left
open must not keep aipager unlocked.

**When the quiet spell is over** (checked every 30 seconds) aipager:

- deletes the messages it knows of in every served chat (sent and
  received; Telegram allows it for 48 hours),
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

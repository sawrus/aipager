# Security audit, November 2026 (fork `sawrus/aipager`)

An audit of the fork on top of 1.3.10: the idle lock the fork adds
([idle lock](idle-lock.md)), what reaches the journal, the modes of the
secret folders, and a review of the rest of the attack surface. Each
finding has a severity, what this branch (`fix/security-audit`) fixed,
and what is left and why.

Severity: **High** — someone outside the trust boundary can act or read;
**Medium** — a control does less than it says, or private content
leaks to a place with a wider audience; **Low** — defence in depth, or
needs another local user or an unusual setup.

## Summary

| # | Finding | Severity | State |
|---|---|---|---|
| 1 | The idle lock leaves messages in the chat | Medium | Fixed |
| 2 | Prompt text in the journal | Medium | Fixed |
| 3 | `~/.config/aipager` and `~/.local/state/aipager` stay `0775` | Low | Fixed |
| 4 | The audit log is created at the umask's mode | Low | Fixed |
| 5 | Legacy personal mode lets commands and taps from any chat through | Low | Left |
| 6 | Mini App `initData` can be replayed for 5 minutes | Low | Left (by design) |
| 7 | `aipager update` / `/update` install upstream's PyPI build | Low | Left |
| 8 | No global token filter on log records | Low | Left |
| 9 | Problem reports go to upstream's Sentry | Info | Left |

## 1. The idle lock leaves messages in the chat — Medium, fixed

The lock's promise is that a person who picks up an unlocked Telegram
session after the quiet spell sees nothing. Before this branch the wipe
missed messages in four ways:

- **The tracker took edits.** The rate limiter noted every result that
  carried a `message_id`, `editMessageText` included. A streaming busy
  card is edited every few seconds, so one card filled the 3000-entry
  tracker (`MAX_TRACKED`) and pushed out real messages; each edit also
  made the card "young" again.
- **It cleared the list before deleting.** `IdleLock.lock()` emptied the
  tracked list and saved before any delete ran. A delete that failed, or
  a daemon stopped mid-wipe, lost those ids for good.
- **Failures were silent.** A failed single delete was logged at `DEBUG`;
  nothing at `INFO` said how much a wipe cleared.
- **`aipager lock now` lied when already locked.** It set "last activity
  = 0", which does nothing while locked, and printed "aipager locks
  within 30 s".

Fixed in this branch:

- A message is noted once, at its first sight (`(chat, id)` → first
  seen). `editMessage*` results are never noted; a copy's `MessageId`
  (no chat in it) is noted under the call's chat.
- In a private chat (one numbering for both sides) the wipe deletes the
  whole id range from the previous wipe up to the newest tracked id, in
  `deleteMessages` calls of 100, at most `WIPE_SPAN` (3000) ids back —
  the same approach as the menu clean-up. Messages the tracker never saw
  go too.
- `lock()` only marks the state locked and `wipe_pending`. The tracked
  ids are forgotten after the wipe (`wiped()`), except the ones whose
  delete failed: they are tried again at the next wipe while younger
  than 48 hours. A wipe a stop cut short runs again at the next tick.
- One `INFO` line per chat: `idle lock: cleared chat <id>: N ids asked,
  M failed` (counts only, never text).
- `aipager lock now` on a locked aipager sets `wipe_pending` in
  `lock.json`; the daemon sees it at its next tick, clears the chats
  again and re-sends the lock notice. The command says so.
- A reload of `lock.json` written by the CLI keeps the messages the
  daemon noted since its last save.

Left: in a group the bot can delete only messages it tracked (ids in a
group are not a private sequence, and the bot sees only messages
addressed to it), and Telegram refuses to delete anything older than
48 hours. Both are Telegram limits, stated in [idle lock](idle-lock.md).

## 2. Prompt text in the journal — Medium, fixed

Nine log calls wrote the first 50-80 characters of what a person sent
(`Sent text`, `Held`, `Template sent`, `Direct send`, `Flushed queued`,
`Retry`, `Voice injected`, `Refused a slash command`, `Dropped text`,
the dtach `Sent text ... + Enter`) and one wrote a queued prompt with
`%r` (`queue full`). The journal keeps them for weeks, other admins of
the machine can read it, and people paste it into bug reports.

Fixed: these lines now log `aipager.text_shape.shape(text)`: the kind
and length (`text, 42 chars`, or `/model, 12 chars` for a bare command
name, never its arguments). `tests/test_log_privacy.py` scans every log
call in `aipager/` and fails when one takes a variable that holds a
person's text (`text`, `prompt`, `transcript`, ...), and checks the
dtach line end to end. A dropped callback toast no longer logs its text.

## 3. Secret folders stay `0775` — Low, fixed

`write_private(..., dir_mode=0o700)` passed the mode only to `mkdir`,
which applies it only when it creates the folder (and the umask narrows
even that). A `~/.config/aipager` made by an older install stayed
`0775`, and `write_secret` (the TOTP secret) passed no `dir_mode` at
all. The files in it are `0600`, but `aipager.yaml` was written at the
umask's mode and narrowed after the write, so for that moment a
`0775` folder let another local user open the bot token.

Fixed: `private_file.secure_dir()` narrows an existing folder of ours
to the mode (never one another user owns). `write_private` calls it
whenever a `dir_mode` is given (`lock.json`, `menu.json`, the TOTP
secret), `aipager.yaml`'s writers narrow its folder, and `aipager start`
narrows `~/.config/aipager` at once.

## 4. The audit log at the umask's mode — Low, fixed

`~/.claude/aipager-audit.jsonl` holds every Allow/Deny with a summary
of the tool input (often a shell command) and every `AskUserQuestion`
answer. It was opened with `open("a")`, so it was `0664` under the usual
umask, in a `~/.claude` that Claude Code may leave `0775`.

Fixed: the audit file is created `0600` and narrowed to `0600` when an
older one is wider.

## 5. Legacy personal mode lets commands from any chat through — Low, left

Command and callback handlers are not chat-filtered; `_authorize` and
`_authorize_callback` check the sender. With scopes or a team that
check is sound. With neither (no `aipager.yaml`, no `team.yaml`: the
legacy personal mode) `_authorize` returns `True` and
`_authorize_callback` returns a sentinel for any chat: a stranger's
`/status` or `/kill` in their own private chat would be acted on.

Why left: the daemon writes `aipager.yaml` from the legacy settings at
every start (`migrate_to_v2` / `upgrade_to_v3`), so the mode is reached
only when that write fails (a read-only config folder). A fix (compare
the chat with `CHAT_ID` in both methods) touches the test fixtures that
drive handlers in personal mode with another chat id, and belongs in
its own change. Recommended next step.

## 6. Mini App `initData` replay window — Low, by design

`verify_init_data` checks the HMAC with a constant-time compare, fails
closed on any unexpected error, and accepts `auth_date` 300 s back and
a small skew forward. A captured `X-Telegram-Init-Data` header (for
example from a shared screen or a proxy log) works for those 5 minutes.
Telegram gives no nonce, so the window is the control. The idle lock
does not change it: a locked aipager answers every route 423, and only
a write counts as activity.

## 7. `/update` installs upstream's build — Low, left

`aipager update` and the admin `/update` read the newest version from
PyPI (`aipager`, published by upstream) and install it. On the fork this
replaces the fork's code, the idle lock included, by upstream's next
release. The fork keeps its version at 1.3.10 so the check never sees a
fork release as older, but an upstream release would still be offered.
Left: the fork has no package index of its own. Do not run `/update` on
a fork install; update from the fork's repository.

## 8. No global token filter on logs — Low, left

The bot token is in every Bot API URL. `httpx` is set to `WARNING` (it
logs URLs at `INFO`), and the preflight, doctor and wizard paths call
`redact_token`. Nothing filters every log record, so a future
`exc_info=True` on an exception whose text holds the URL would print
the token. Left as a recommendation: a `logging.Filter` on the root
handler that applies `redact_bare_token`.

## 9. Problem reports go to upstream's Sentry — Info

A report leaves the machine only after the person previews it and taps
Send, and it holds only allow-listed fixed-shape fields (no text, ids,
paths or tokens; [problem reports](problem-reports.md)). The endpoint is
read from upstream's GitHub (`dev-aly3n/aipager`) at send time, so a
fork user's report reaches the upstream maintainer. Nothing is fetched
at start or on a timer.

## Checked, no finding

- **Authorization with scopes.** Every command handler (`/start` ...
  `/update`, `/restart`, `/rename`, `/delete`, `/diff`) calls
  `_authorize` first; message, file and voice handlers also sit behind
  the chat filter; taps go through `_authorize_callback`, which checks
  membership in the tapped message's chat. The idle lock's gate (group
  -4) runs before all of them and accepts a code only in a member's
  private chat.
- **Uploads** (`aipager/uploads.py`): per-chat `0700` folders named by an
  HMAC of the chat id; symlinks, foreign owners and non-folders are
  refused; each file is created `O_EXCL | O_NOFOLLOW`, `0600`; the file
  name is reduced to its last path part.
- **TOTP**: RFC 6238 test vectors, constant-time compare, one step each
  way, each step accepted once, five wrong codes in ten minutes pause
  the checks, the code message is deleted.

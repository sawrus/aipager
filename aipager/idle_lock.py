"""Idle lock: after a quiet spell the bot and the Mini App need a TOTP code.

Telegram access is shell access here: whoever holds the owner's Telegram
account (a lost phone, an open desktop session) can drive every Claude
session. The idle lock bounds that window. When no member has done
anything for ``after_min`` minutes, the daemon:

- deletes the messages it knows of (Telegram allows it for 48 hours),
- stops every outbound Bot API call that carries a chat (session output,
  cards, prompts) — :func:`refuses`, checked in the rate limiter, the one
  place every outbound call passes,
- drops every inbound update except ``/start`` and a 6-digit code in a
  member's private chat (``aipager.bot.idle_gate``),
- answers every Mini App route with 423.

A code from an authenticator app (RFC 6238, 30 s steps, SHA-1, 6 digits)
unlocks it. A step is accepted once; five wrong codes in ten minutes pause
the checks.

Settings live in ``aipager.yaml`` (``security.idle_lock``, see
:func:`aipager.scope.load_idle_lock`), the secret in ``totp.env`` (0600),
the state in ``lock.json`` (0600). ``aipager lock`` manages all three.

This module imports nothing from ``aipager.bot`` or ``aipager.i18n``: the
rate limiter and the Mini App use it, and the CLI runs it cold.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import re
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

from aipager._test_guard import check_write

log = logging.getLogger(__name__)

LOCK_STATE_PATH = Path.home() / ".local" / "state" / "aipager" / "lock.json"
TOTP_PATH = Path.home() / ".config" / "aipager" / "totp.env"

TOTP_STEP = 30
TOTP_DIGITS = 6
MAX_FAILS = 5
FAIL_WINDOW = 600
#: Telegram lets a bot delete a message for 48 hours after it was sent.
WIPE_WINDOW = 48 * 3600
#: Message ids kept for the wipe, newest last.
MAX_TRACKED = 3000

#: Bot API methods that never carry session content: allowed while locked.
#: Deleting is how the lock clears the chat; the rest have no chat.
ALWAYS_ALLOWED = frozenset({
    "deleteMessage", "deleteMessages", "answerCallbackQuery",
    "getMe", "getUpdates", "getFile", "getChat",
})

_CODE_RE = re.compile(r"^\s*(\d{3})\s?(\d{3})\s*$")
_SECRET_KEY = "TOTP_SECRET"

# Set while the lock itself talks to Telegram (the lock notice, the code
# prompt): those calls pass the gate. A ContextVar, so a session task that
# runs at the same moment never inherits it.
_service = contextvars.ContextVar("aipager_idle_lock_service", default=False)


# ---- TOTP (RFC 4226 / RFC 6238) ---------------------------------------------

def totp_code(secret: str, step: int, digits: int = TOTP_DIGITS) -> str:
    """The HMAC-SHA1 code of *secret* (base32) for time step *step*."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def verify_code(secret: str, code: str, now: float, last_step: int):
    """Check *code* against the previous, current and next step.

    Returns ``(step, "ok")``, ``(None, "replayed")`` for a step already
    used (or older), or ``(None, "denied")``."""
    current = int(now // TOTP_STEP)
    for step in range(current - 1, current + 2):
        if hmac.compare_digest(totp_code(secret, step), code):
            return (step, "ok") if step > last_step else (None, "replayed")
    return None, "denied"


def parse_code(text) -> str | None:
    """``"123456"`` or ``"123 456"`` → ``"123456"``; anything else → None."""
    match = _CODE_RE.match(text or "")
    return match.group(1) + match.group(2) if match else None


def new_secret() -> str:
    """A fresh 160-bit base32 secret, without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def otpauth_uri(secret: str, account: str = "aipager") -> str:
    """The ``otpauth://`` URI an authenticator app imports."""
    return (f"otpauth://totp/aipager:{quote(account)}"
            f"?secret={secret}&issuer=aipager&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def read_secret(path: Path | None = None) -> str:
    """The secret from ``totp.env``, or ``""`` when absent or unreadable."""
    path = path or TOTP_PATH
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip() == _SECRET_KEY:
                return value.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def write_secret(secret: str, path: Path | None = None) -> None:
    from aipager.private_file import write_private

    write_private(path or TOTP_PATH,
                  "# aipager idle lock: TOTP secret. Keep private.\n"
                  f"{_SECRET_KEY}={secret}\n")


# ---- state -------------------------------------------------------------------

def _fresh_state() -> dict:
    return {"locked": False, "last_activity": 0.0, "last_step": 0,
            "fails": [], "sent": []}


class IdleLock:
    """The lock's state, shared by the bot, the rate limiter and the Mini App.

    One instance per process (:data:`LOCK`). The daemon keeps the state in
    memory and writes it on every lock change and, for activity, at most
    once per tick (:meth:`save_if_dirty`). ``aipager lock`` edits the same
    file; :meth:`reload_if_changed` picks that up."""

    def __init__(self, state_path: Path | None = None, totp_path: Path | None = None,
                 settings_loader=None, clock=time.time) -> None:
        self._state_path = state_path
        self._totp_path = totp_path
        self._settings_loader = settings_loader
        self._clock = clock
        self._state = _fresh_state()
        self._mtime: float | None = None
        self._dirty = False
        self._loaded = False
        self._cached: tuple[float, bool, dict] | None = None
        self._warned = False

    # Paths resolve late, so a test that patches the module constants
    # reaches an instance built at import time.
    @property
    def state_path(self) -> Path:
        return self._state_path or LOCK_STATE_PATH

    @property
    def totp_path(self) -> Path:
        return self._totp_path or TOTP_PATH

    #: Seconds the settings and the secret's presence are reused: every
    #: outbound Bot API call asks :meth:`is_locked`.
    CACHE_SECONDS = 10.0

    def _read_settings(self) -> dict:
        if self._settings_loader is not None:
            return self._settings_loader()
        from aipager.scope import load_idle_lock
        return load_idle_lock()

    def _cached_view(self) -> tuple[bool, dict]:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > self.CACHE_SECONDS:
            settings = self._read_settings()
            self._cached = (now, bool(self.secret()), settings)
        return self._cached[1], self._cached[2]

    def forget_cache(self) -> None:
        self._cached = None

    def settings(self) -> dict:
        return self._cached_view()[1]

    def secret(self) -> str:
        return read_secret(self.totp_path)

    @property
    def enabled(self) -> bool:
        """On in ``aipager.yaml`` and a secret exists. Without a secret
        nobody could ever unlock, so the lock stays off (and says so)."""
        has_secret, settings = self._cached_view()
        if not settings["enabled"]:
            return False
        if not has_secret:
            if not self._warned:
                log.warning("idle lock: enabled in aipager.yaml but there is no TOTP "
                            "secret; run `aipager lock setup`. The lock is OFF.")
                self._warned = True
            return False
        return True

    # -- persistence --

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def load(self) -> None:
        state = _fresh_state()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update({k: raw[k] for k in state if k in raw})
            self._mtime = self.state_path.stat().st_mtime
        except (OSError, ValueError):
            self._mtime = None
        self._state = state
        self._loaded = True
        self._dirty = False

    def save(self) -> None:
        from aipager.private_file import write_private

        try:
            check_write(self.state_path)
            write_private(self.state_path, json.dumps(self._state), dir_mode=0o700)
            self._mtime = self.state_path.stat().st_mtime
            self._dirty = False
        except OSError:
            log.warning("idle lock: could not save %s", self.state_path, exc_info=True)

    def save_if_dirty(self) -> None:
        if self._dirty:
            self.save()

    def reload_if_changed(self) -> None:
        """Re-read the file when someone else (``aipager lock``) wrote it."""
        try:
            mtime = self.state_path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            self.load()

    # -- queries --

    def is_locked(self) -> bool:
        self._ensure_loaded()
        return bool(self._state["locked"]) and self.enabled

    @property
    def locked(self) -> bool:
        """The stored state alone, whether or not the lock is enabled."""
        self._ensure_loaded()
        return bool(self._state["locked"])

    @property
    def last_activity(self) -> float:
        self._ensure_loaded()
        return float(self._state["last_activity"] or 0)

    def due(self, now: float | None = None) -> bool:
        """Enabled, not locked yet, and quiet for longer than ``after_min``."""
        self._ensure_loaded()
        if self._state["locked"] or not self.enabled:
            return False
        now = self._clock() if now is None else now
        return now - self.last_activity > self.settings()["after_min"] * 60

    # -- changes --

    def make_due(self) -> None:
        """Forget the last activity: the daemon locks at its next tick."""
        self._ensure_loaded()
        self._state["last_activity"] = 0.0
        self.save()

    def touch(self, now: float | None = None) -> None:
        """A member did something: the quiet clock starts again."""
        self._ensure_loaded()
        self._state["last_activity"] = self._clock() if now is None else now
        self._dirty = True

    def note_message(self, chat_id, message_id, now: float | None = None) -> None:
        """Remember a message in a served chat, for the wipe at the next lock."""
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        if not self.enabled:
            return
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return
        self._ensure_loaded()
        now = self._clock() if now is None else now
        self._state["sent"] = (self._state["sent"] + [[chat_id, message_id, int(now)]])[-MAX_TRACKED:]
        self._dirty = True

    def lock(self, now: float | None = None) -> dict[int, list[int]]:
        """Lock now. Returns the messages to delete, by chat (younger than
        48 hours), and forgets them."""
        self._ensure_loaded()
        now = self._clock() if now is None else now
        wipe: dict[int, list[int]] = {}
        for chat_id, message_id, ts in self._state["sent"]:
            if now - ts < WIPE_WINDOW:
                wipe.setdefault(int(chat_id), []).append(int(message_id))
        self._state.update(locked=True, sent=[], fails=[])
        self.save()
        log.info("idle lock: locked")
        return {chat: sorted(set(ids)) for chat, ids in wipe.items()}

    def unlock(self, now: float | None = None) -> None:
        self._ensure_loaded()
        now = self._clock() if now is None else now
        self._state.update(locked=False, last_activity=now, fails=[])
        self.save()
        log.info("idle lock: unlocked")

    def try_code(self, text, now: float | None = None) -> str:
        """Check a code sent while locked: ``"ok"`` (unlocked), ``"denied"``,
        ``"replayed"``, ``"throttled"`` or ``"not_a_code"``."""
        self._ensure_loaded()
        code = parse_code(text)
        if code is None:
            return "not_a_code"
        now = self._clock() if now is None else now
        fails = [t for t in self._state["fails"] if now - t < FAIL_WINDOW]
        if len(fails) >= MAX_FAILS:
            self._state["fails"] = fails
            self.save()
            return "throttled"
        step, verdict = verify_code(self.secret(), code, now, int(self._state["last_step"]))
        if verdict == "ok":
            self._state["last_step"] = step
            self.unlock(now)
            return "ok"
        self._state["fails"] = fails + [now]
        self.save()
        log.warning("idle lock: a wrong code (%s)", verdict)
        return verdict


LOCK = IdleLock()


@contextlib.contextmanager
def service():
    """Let the lock's own calls (notice, code prompt) through the gate."""
    token = _service.set(True)
    try:
        yield
    finally:
        _service.reset(token)


def refuses(endpoint: str, chat_id) -> bool:
    """Whether the rate limiter must refuse this outbound call."""
    if chat_id is None or endpoint in ALWAYS_ALLOWED or _service.get():
        return False
    return LOCK.is_locked()


def record_result(result) -> None:
    """Remember the message(s) a Bot API call returned, for the next wipe."""
    if isinstance(result, dict) and "result" in result:  # a raw API response
        result = result["result"]
    items = result if isinstance(result, list) else [result]
    for item in items:
        if isinstance(item, dict) and "message_id" in item:
            chat = item.get("chat")
            if isinstance(chat, dict):
                LOCK.note_message(chat.get("id"), item["message_id"])

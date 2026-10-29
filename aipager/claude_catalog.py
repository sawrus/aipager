"""Claude Code conversations started without aipager (fork ``sawrus/aipager``).

A person runs ``claude`` in a plain terminal; aipager never saw it. This
module finds such conversations in Claude Code's own files so ``/resume``
and ``aipager resume --claude`` can offer them, and tells whether a live
terminal claude still holds one (it must be closed before aipager can
resume the conversation under dtach: aipager only drives its own PTYs).

Reads only; never raises (an unreadable file or folder reads as "nothing
known"). Imports nothing from ``aipager.bot`` or ``aipager.i18n``.

CONTRACT (shared by the catalog, the adopt flow, the Telegram picker and
the CLI; change it only together with every user):

- :class:`ForeignConversation`: one conversation, newest activity first in
  every list.
- :func:`list_foreign`: conversations with activity in the last *days*
  days that aipager does not track, at most *limit*.
- :func:`holder`: the live interactive claude holding a conversation.
- :class:`AdoptOutcome`: what ``SessionOpsMixin.adopt_conversation``
  returns.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aipager import claude_jobs

#: How much of a transcript's head is read for the cwd and the title.
HEAD_READ_BYTES = 64 * 1024
#: The longest title kept.
TITLE_MAX = 80

#: Every reason :class:`AdoptOutcome` may carry.
ADOPT_REASONS = frozenset({
    "ok",              # the conversation now runs under aipager (``label``)
    "needs_takeover",  # a terminal claude holds it and waits: ask, then confirm=True
    "terminal_busy",   # a terminal claude holds it and is working: nothing done
    "takeover_failed", # the terminal claude did not exit after SIGTERM: nothing done
    "forbidden",       # the caller is not the chat's owner or an admin
    "not_found",       # no such conversation (or no transcript left)
    "launch_failed",   # the resume under dtach did not start (``err`` says why)
})


@dataclass(frozen=True)
class ForeignConversation:
    """A Claude Code conversation aipager does not track."""

    #: Claude Code's conversation id (a UUID), the transcript's file stem.
    session_id: str
    #: The folder the conversation ran in ("" when the transcript does not say).
    cwd: str
    #: A custom title or summary, else the first words of the first user
    #: message; plain text, at most 80 characters ("" when none).
    title: str
    #: Unix time of the last record in the transcript.
    last_ts: float
    #: The pid of a live interactive claude holding it, else None.
    holder_pid: int | None = None
    #: That claude's registry status ("idle", "busy", "waiting", ...), else "".
    holder_status: str = ""


@dataclass
class AdoptOutcome:
    """Result of ``SessionOpsMixin.adopt_conversation``."""

    ok: bool
    #: One of :data:`ADOPT_REASONS`.
    reason: str
    #: The aipager label the conversation runs under (``ok``), else "".
    label: str = ""
    #: The holder's status (``needs_takeover``, ``terminal_busy``), else "".
    holder_status: str = ""
    #: Free-form detail for logs (``launch_failed``, ``takeover_failed``).
    err: str = ""


# Record types that carry a title, best first: a title the person chose,
# a summary Claude wrote, the title Claude generated.
_TITLE_KEYS = (("custom-title", "customTitle"), ("summary", "summary"),
               ("ai-title", "aiTitle"))
# A "user" record that opens with one of these is Claude Code's own text.
_WRAPPERS = ("<command-", "<local-command", "<system-reminder", "<bash-",
             "Caveat:", "[Request interrupted")


def _tracked_ids() -> set[str]:
    """Every ``claude_session_id`` in aipager's persisted session state."""
    from aipager.status import _read_state
    sessions = _read_state().get("sessions", {})
    if not isinstance(sessions, dict):
        return set()
    return {sd["claude_session_id"] for sd in sessions.values()
            if isinstance(sd, dict) and isinstance(sd.get("claude_session_id"), str)}


def _head_records(path: Path) -> Iterator[dict]:
    """The JSON records among the lines of the first
    :data:`HEAD_READ_BYTES` of *path* (a line cut by the limit is not JSON
    and is skipped)."""
    with open(path, "rb") as f:
        data = f.read(HEAD_READ_BYTES)
    for line in data.split(b"\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _user_text(record: dict) -> str:
    """The text a person typed in a ``user`` record; "" for tool results,
    meta records, sidechains and Claude Code's own wrapper text."""
    if (record.get("type") != "user" or record.get("isMeta")
            or record.get("isSidechain")):
        return ""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text"
                           and isinstance(b.get("text"), str))
    if not isinstance(content, str):
        return ""
    text = " ".join(content.split())
    return "" if text.startswith(_WRAPPERS) else text


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "\u2026"


def _head_facts(path: Path) -> tuple[str, str]:
    """``(cwd, title)`` from the head of the transcript at *path*."""
    cwd = ""
    titles: dict[str, str] = {}
    first_user = ""
    for record in _head_records(path):
        if not cwd and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        for kind, key in _TITLE_KEYS:
            value = record.get(key)
            if record.get("type") == kind and isinstance(value, str) and value.strip():
                titles[kind] = value        # the last one wins
        if not first_user:
            first_user = _user_text(record)
    for kind, _key in _TITLE_KEYS:
        if kind in titles:
            return cwd, _clip(titles[kind])
    return cwd, _clip(first_user)


def _last_ts(path: Path, mtime: float) -> float:
    """The time of the transcript's last record, else *mtime*."""
    try:
        line = claude_jobs._last_complete_line(str(path))
        stamp = json.loads(line).get("timestamp") if line else None
        if isinstance(stamp, str):
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, AttributeError):
        pass
    return mtime


def list_foreign(days: int = 14, limit: int = 200) -> list[ForeignConversation]:
    """Conversations with activity in the last *days* days that aipager
    does not track, newest first, at most *limit*."""
    found: list[tuple[float, Path]] = []
    cutoff = time.time() - days * 86400
    try:
        for path in (claude_jobs.config_dir() / "projects").glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff and claude_jobs.valid_session_id(path.stem):
                found.append((mtime, path))
    except OSError:
        return []
    found.sort(key=lambda x: x[0], reverse=True)
    try:
        tracked = _tracked_ids()
    except Exception:                    # state unreadable: nothing known
        tracked = set()
    out: list[ForeignConversation] = []
    for mtime, path in found:
        if len(out) >= limit:
            break
        if path.stem in tracked or claude_jobs.transcript_continued_in(path):
            continue
        try:
            cwd, title = _head_facts(path)
        except OSError:
            continue
        held = holder(path.stem)
        out.append(ForeignConversation(
            session_id=path.stem, cwd=cwd, title=title,
            last_ts=_last_ts(path, mtime),
            holder_pid=held["pid"] if held else None,
            holder_status=held["status"] if held else ""))
    out.sort(key=lambda c: c.last_ts, reverse=True)
    return out


def holder(session_id: str) -> dict | None:
    """The live interactive claude holding *session_id*:
    ``{"pid": int, "status": str, "name": str}``, or None."""
    try:
        entries = claude_jobs.read_registry() or []
    except Exception:
        return None
    for e in entries:
        if (e.get("sessionId") == session_id
                and e.get("kind") == claude_jobs._INTERACTIVE
                and claude_jobs._pid_alive(e["pid"])
                and claude_jobs._start_matches(e, e["pid"])):
            return {"pid": e["pid"], "status": e.get("status", ""),
                    "name": e.get("name", "")}
    return None

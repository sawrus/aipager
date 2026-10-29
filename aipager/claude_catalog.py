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

from dataclasses import dataclass

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


def list_foreign(days: int = 14, limit: int = 200) -> list[ForeignConversation]:
    """Conversations with activity in the last *days* days that aipager
    does not track, newest first, at most *limit*."""
    raise NotImplementedError


def holder(session_id: str) -> dict | None:
    """The live interactive claude holding *session_id*:
    ``{"pid": int, "status": str, "name": str}``, or None."""
    raise NotImplementedError

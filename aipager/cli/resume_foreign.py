"""`aipager resume --claude` (fork ``sawrus/aipager``): pick a Claude Code
conversation started without aipager and continue it under aipager.

The terminal mirror of the Telegram picker's "Other Claude sessions"
(``aipager.claude_catalog``). A terminal claude still holding the
conversation is closed after ``[y/N]`` (SIGTERM, never while it works).
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import time

from aipager import instance
from aipager.cli.resume import _MODE_NAMES, _RESUME_PAGE_SIZE, _fmt_ago_cli


def add_flag(resume_p: argparse.ArgumentParser) -> None:
    """The ``--claude`` flag on ``aipager resume``."""
    resume_p.add_argument(
        "--claude", action="store_true",
        help="pick a Claude Code conversation started without aipager "
             "(last 14 days) and continue it under aipager",
    )


def cmd(args: argparse.Namespace) -> int:
    """``aipager resume --claude``: the picker; a session name is refused."""
    if args.name:
        from aipager.errors import friendly_error
        friendly_error("--claude takes no session name.",
                       "  Run `aipager resume --claude` and pick from the list.")
        return 2
    return _foreign_picker_loop()


#: How long a terminal claude gets to exit after SIGTERM.
_TAKEOVER_WAIT = 10.0
#: How far back the listing looks.
_FOREIGN_DAYS = 14


def _sleep(seconds: float) -> None:
    """Seam: tests replace it (never ``time.sleep`` through a module path)."""
    time.sleep(seconds)


def _clock() -> float:
    """Seam: tests replace it."""
    return time.monotonic()


def _terminate(pid: int) -> None:
    """Seam: ask the terminal claude *pid* to exit (tests replace it)."""
    os.kill(pid, signal.SIGTERM)


def _pid_alive(pid: int) -> bool:
    """Seam: whether *pid* still runs (tests replace it)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _foreign_title(conv) -> str:
    """Its title, else its folder's name, else the start of its id."""
    return (conv.title or os.path.basename((conv.cwd or "").rstrip("/"))
            or conv.session_id[:8])


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _home_short(path: str) -> str:
    home = os.path.expanduser("~").rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def _holder_marker(status: str, pid) -> str:
    if pid is None and not status:
        return ""
    return "[terminal: working]" if status == "busy" else "[terminal: waiting]"


def _foreign_label(cwd: str) -> str:
    """The aipager label for a conversation run in *cwd*: the folder's
    name, normalized like a typed name, ``[a-z0-9_]`` only, not a reserved
    command and not the name of a live session."""
    from aipager.dtach.inject import _RESERVED, normalize_session_name
    from aipager.status import _live_sessions
    base = normalize_session_name(os.path.basename((cwd or "").rstrip("/")))
    base = re.sub(r"[^a-z0-9_]+", "_", base).strip("_")[:30] or "claude"
    taken = {n.removeprefix("claude-") for n in _live_sessions()}
    label, n = base, 1
    while label in _RESERVED or label in taken:
        n += 1
        label = f"{base}_{n}"
    return label


def _foreign_picker_loop() -> int:
    """`aipager resume --claude`: the Claude Code conversations of the last
    two weeks that aipager does not run, ten a page; a number continues
    one under aipager (:func:`_adopt_foreign`)."""
    from aipager import claude_catalog
    items = claude_catalog.list_foreign(days=_FOREIGN_DAYS)
    if not items:
        from aipager.ui import console
        console.print("[muted]No other Claude Code conversations in the last "
                      f"{_FOREIGN_DAYS} days.[/muted]")
        return 0

    page = 0
    page_size = _RESUME_PAGE_SIZE
    total_pages = (len(items) + page_size - 1) // page_size

    while True:
        start = page * page_size
        chunk = items[start:start + page_size]
        print()
        print(f"Claude Code conversations started without aipager - page "
              f"{page + 1}/{total_pages} ({len(items)} total)")
        print()
        for i, conv in enumerate(chunk, start=1):
            title = _cut(_foreign_title(conv), 30)
            folder = _cut(_home_short(conv.cwd or ""), 30)
            row = f"  {i:>2}. {title:<30} {folder:<30} {_fmt_ago_cli(conv.last_ts):>8}"
            marker = _holder_marker(conv.holder_status, conv.holder_pid)
            if marker:
                row += f"  {marker}"
            print(row)
        print()
        prompt = "Pick a number"
        if page > 0:
            prompt += ", [p]rev"
        if page < total_pages - 1:
            prompt += ", [n]ext"
        prompt += ", [q]uit: "
        try:
            choice = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", ""):
            return 0
        if choice in ("n", "next"):
            if page < total_pages - 1:
                page += 1
            continue
        if choice in ("p", "prev"):
            if page > 0:
                page -= 1
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(chunk):
                return _adopt_foreign(chunk[idx])
        # Unrecognized input — re-loop with the same page


def _adopt_foreign(conv) -> int:
    """Continue *conv* under aipager: ``claude --resume <id>`` under dtach
    in its folder. A terminal claude holding it is closed first, only
    after the person says yes, and only while it waits (a working one is
    left alone). Returns the shell exit code."""
    import asyncio as _asyncio
    from pathlib import Path

    from aipager import claude_catalog, perm_modes
    from aipager.dtach import inject as dtach_inject
    from aipager.errors import friendly_error
    from aipager.ui import ok as ui_ok

    title = _foreign_title(conv)
    held = claude_catalog.holder(conv.session_id)
    if held is not None:
        if held.get("status") == "busy":
            friendly_error(f"{title!r} is working in the terminal right now.",
                           "  Try again when it waits.")
            return 1
        try:
            answer = input("Close it in the terminal and continue under aipager? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if answer.strip().lower() not in ("y", "yes"):
            return 0
        # Asked again: it may have started working while the person read.
        held = claude_catalog.holder(conv.session_id)
        if held is not None:
            if held.get("status") == "busy":
                friendly_error(f"{title!r} is working in the terminal right now.",
                               "  Try again when it waits.")
                return 1
            pid = int(held["pid"])
            _terminate(pid)
            deadline = _clock() + _TAKEOVER_WAIT
            while _pid_alive(pid):
                if _clock() >= deadline:
                    friendly_error(f"the terminal claude (pid {pid}) did not exit; "
                                   "nothing changed.")
                    return 1
                _sleep(0.2)

    # Where the conversation moved, and a Claude Code background job
    # holding it, exactly as `aipager resume <name>` checks.
    from aipager.dtach.launcher import prepare_resume
    target, rc = prepare_resume(conv.session_id)
    if target is None:
        return rc
    label = _foreign_label(conv.cwd)
    mode = perm_modes.AUTO
    ok, err = _asyncio.run(dtach_inject.launch_session(
        label, resume_id=target, cwd=conv.cwd or None, mode=mode,
    ))
    if not ok:
        friendly_error(f"couldn't continue {title!r}: {err}")
        return 1
    mode_str = f"{_MODE_NAMES[mode]} ({perm_modes.launch_flag(mode)})"
    ui_ok(f"continued as [path]{label}[/path] "
          f"(session-id {target[:8]}…, mode={mode_str})")
    sock = Path(f"{instance.dtach_sock_prefix()}{label}.sock")
    print(f"\nAttach with: dtach -a {sock}")
    return 0

"""`aipager resume` — bring back a previously-gone Claude Code session.

CLI mirror of the Telegram `/resume` flow. Reads ``aipager-sessions.json``
directly (the daemon owns writes; reads are safe). Output is a
human-friendly table + an instruction to ``dtach -a`` the resumed socket.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import time

from aipager import instance

_RESUME_PAGE_SIZE = 10


def _cmd_resume(args: argparse.Namespace) -> int:
    """`aipager resume [<name>]` — resume a previously-gone Claude session.

    With ``<name>``: directly invoke ``claude --resume`` on that session
    via dtach. Without an arg: render a paginated list of GONE sessions
    and accept ``<number>`` / ``n`` (next) / ``p`` (prev) / ``q`` (quit).

    Prefix the name with ``!`` to force Full access mode
    (--dangerously-skip-permissions) regardless of the persisted value
    (e.g. ``aipager resume !dev``).

    The daemon's session_monitor auto-discovers the new socket within ~2s
    and recovers the registry entry to IDLE; we don't write to the state
    file directly here (the daemon owns it).
    """
    if getattr(args, "claude", False):
        if args.name:
            from aipager.errors import friendly_error
            friendly_error("--claude takes no session name.",
                           "  Run `aipager resume --claude` and pick from the list.")
            return 2
        return _foreign_picker_loop()
    raw_name = (args.name or "").strip().lstrip("@/") if args.name else ""
    if raw_name:
        # Support ! prefix to force Full access mode
        force_full = raw_name.startswith("!")
        name = raw_name.lstrip("!").lower()
        return _resume_one(name, force_full=force_full)
    return _resume_picker_loop()


def _gone_history() -> list[dict]:
    """Read the persisted state file, return GONE sessions newest-first."""
    from aipager.status import _live_sessions, _read_state
    state = _read_state()
    persisted = state.get("sessions", {}) or {}
    live = _live_sessions()
    gone = []
    for n, sd in persisted.items():
        if n in live:
            continue
        if not sd.get("gone_at") and not sd.get("claude_session_id"):
            continue
        gone.append(sd)
    gone.sort(key=lambda s: s.get("gone_at") or 0.0, reverse=True)
    return gone


def _all_records() -> list[dict]:
    """Every persisted session record, live ones included."""
    from aipager.status import _read_state
    persisted = (_read_state().get("sessions", {}) or {}).values()
    return [sd for sd in persisted if sd.get("name")]


def _resolve_record(label: str,
                    records: list[dict]) -> tuple[dict | None, list[str]]:
    """Resolve a user-typed label to one session record.

    Sessions opened from Telegram are named ``claude-<label>__d<chat_id>``,
    so reconstructing ``claude-<label>`` from user input misses them. Match
    on the record instead: exact name first, then the record's own ``label``.

    Returns ``(record, [])`` on a unique hit, ``(None, [names])`` when the
    label is ambiguous across scopes, and ``(None, [])`` when nothing matches.
    """
    named = [sd for sd in records if sd.get("name")]

    exact = f"claude-{label}"
    for sd in named:
        if sd["name"] == exact:
            return sd, []

    by_label = [sd for sd in named if sd.get("label") == label]
    if len(by_label) == 1:
        return by_label[0], []
    if by_label:
        return None, sorted(
            sd["name"].removeprefix("claude-") for sd in by_label
        )
    return None, []


#: How the resumed mode is named when printed (with the flag it launched with).
_MODE_NAMES = {"ask": "Ask", "auto": "Auto", "full": "Full access"}


def _saved_model(sd: dict) -> str | None:
    """The model the session was started on or switched to (``launch_model``),
    as the Telegram resume and restart pass it, or ``None`` for Claude
    Code's default. Anything but non-blank text counts as none."""
    model = sd.get("launch_model")
    if not isinstance(model, str) or not model.strip():
        return None
    return model.strip()


def _resume_one(label: str, *, force_full: bool = False) -> int:
    """Resume a single session by label. Returns shell exit code.

    The session comes back in its saved mode (``perm_mode``; an older file's
    ``skip_perms`` True is Full access, anything else Ask). ``force_full``
    overrides it and launches with ``--dangerously-skip-permissions``
    (Full access mode). The person at the terminal runs the daemon's own
    account, so no admin rule applies here.
    """
    from aipager import perm_modes
    import asyncio as _asyncio
    from pathlib import Path

    from aipager.dtach import inject as dtach_inject
    from aipager.errors import friendly_error
    from aipager.ui import ok as ui_ok

    sd, ambiguous = _resolve_record(label, _gone_history())

    if sd is None:
        if ambiguous:
            friendly_error(
                f"{label!r} matches more than one session:",
                *(f"    {n}" for n in ambiguous),
                "  Re-run with the full name.",
            )
            return 1
        # Not resumable. It may still be running — _gone_history() excludes
        # live sessions — so resolve against the full set to name its socket.
        live_sd, live_ambiguous = _resolve_record(label, _all_records())
        if live_ambiguous:
            friendly_error(
                f"{label!r} matches more than one session:",
                *(f"    {n}" for n in live_ambiguous),
                "  Re-run with the full name.",
            )
            return 1
        live_label = live_sd["name"].removeprefix("claude-") if live_sd else label
        sock = Path(f"{instance.dtach_sock_prefix()}{live_label}.sock")
        if sock.is_socket():
            friendly_error(
                f"session {live_label!r} is already running.",
                f"  Socket: {sock}",
                "  Attach it directly:",
                f"    dtach -a {sock}",
            )
            return 1
        friendly_error(
            f"no session named {label!r} in history.",
            "  Run `aipager resume` with no arg to see what's available.",
        )
        return 1

    # Everything downstream keys off the resolved name, never the raw input:
    # the socket and the label dtach launches under both carry the scope suffix.
    full_label = sd["name"].removeprefix("claude-")
    sock = Path(f"{instance.dtach_sock_prefix()}{full_label}.sock")

    if sock.is_socket():
        friendly_error(
            f"session {full_label!r} is already running.",
            f"  Socket: {sock}",
            "  Attach it directly:",
            f"    dtach -a {sock}",
        )
        return 1

    resume_id = sd.get("claude_session_id") or ""
    cwd = sd.get("cwd") or ""
    if not resume_id:
        friendly_error(
            f"session {full_label!r} has no resumable transcript on disk.",
            f"  Start a fresh one with: aipager session {label}",
        )
        return 1
    # Where the conversation moved, and a Claude Code background job
    # holding it (R3): an idle one is stopped, a busy one refuses.
    from aipager.dtach.launcher import prepare_resume
    target, rc = prepare_resume(resume_id)
    if target is None:
        return rc
    resume_id = target
    # The persisted mode unless `!` forces Full access.
    mode = perm_modes.FULL if force_full else perm_modes.mode_from_saved(sd)
    ok, err = _asyncio.run(dtach_inject.launch_session(
        full_label, resume_id=resume_id, cwd=cwd or None,
        mode=mode, is_relaunch=True, model=_saved_model(sd),
    ))
    if not ok:
        friendly_error(f"couldn't resume {full_label!r}: {err}")
        return 1
    mode_str = f"{_MODE_NAMES[mode]} ({perm_modes.launch_flag(mode)})"
    ui_ok(f"resumed [path]{full_label}[/path] "
          f"(session-id {resume_id[:8]}…, mode={mode_str})")
    preview = sd.get("last_assistant_preview") or ""
    if preview:
        from aipager.ui import console
        console.print()
        console.print("[muted]Last response:[/muted]")
        # Claude's own words, as written: a Markdown link or a "[/x]" is
        # not markup, and a ":smile:" is not an emoji code.
        console.print(f"  {preview}", markup=False, emoji=False)
    print(f"\nAttach with: dtach -a {sock}")
    return 0


def _resume_picker_loop() -> int:
    """Paginated picker for `aipager resume` with no name argument."""
    gone = _gone_history()
    if not gone:
        from aipager.ui import console
        console.print("[muted]No previous sessions to resume.[/muted]")
        return 0

    page = 0
    page_size = _RESUME_PAGE_SIZE
    total_pages = (len(gone) + page_size - 1) // page_size

    while True:
        start = page * page_size
        chunk = gone[start:start + page_size]
        print()
        print(f"Previous sessions - page {page + 1}/{total_pages} "
              f"({len(gone)} total)")
        print()
        for i, sd in enumerate(chunk, start=1):
            label = sd.get("label", sd.get("name", "?"))
            gone_at = sd.get("gone_at")
            ago = _fmt_ago_cli(gone_at)
            preview = sd.get("last_assistant_preview") or ""
            row = f"  {i:>2}. {label:<20} {ago}"
            if preview:
                snippet = preview[:60] + ("…" if len(preview) > 60 else "")
                row += f"  - {snippet}"
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
                # Pass the full name: the picker already knows exactly which
                # record was chosen, so don't re-resolve a possibly ambiguous
                # short label.
                name = chunk[idx].get("name", "")
                if name:
                    return _resume_one(name.removeprefix("claude-"))
        # Unrecognized input — re-loop with the same page


def _fmt_ago_cli(gone_at) -> str:
    """Short relative timestamp for the CLI picker."""
    import time as _t
    try:
        gone_at = float(gone_at) if gone_at else 0.0
    except (TypeError, ValueError):
        return "?"
    if not gone_at:
        return "?"
    delta = max(0, int(_t.time() - gone_at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ---- `aipager resume --claude` (fork): conversations started without aipager

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

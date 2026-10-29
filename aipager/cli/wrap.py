"""``aipager-claude``: the ``claude`` command that runs under aipager.

``aipager wrap install`` puts ``alias claude=aipager-claude`` in the
interactive part of ``~/.bashrc``. The wrapper then starts every plain
``claude`` in a terminal as an aipager session in the current folder, so
Telegram sees and drives it. It runs the REAL claude directly (resolved
like aipager does, never by the bare name the alias shadows) when a
session would be wrong: already inside one, no terminal, a one-shot or
info flag, a Claude subcommand, or no daemon.

No recursion: aipager launches claude by its full path
(:func:`aipager.claude_resolve.resolve_claude_binary`), and an alias is
never expanded outside an interactive shell.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from aipager._test_guard import check_write

BASHRC_PATH = Path.home() / ".bashrc"
BLOCK_START = "# >>> aipager wrap >>>"
BLOCK_END = "# <<< aipager wrap <<<"
_ALIAS_LINE = "[[ $- == *i* ]] && alias claude=aipager-claude"
BLOCK = f"{BLOCK_START}\n{_ALIAS_LINE}\n{BLOCK_END}\n"

SESSION_ENV = "CLAUDE_DTACH_SESSION"
_PASS_FLAGS = frozenset({"-p", "--print", "-v", "--version", "-h", "--help"})
_SUBCOMMANDS = frozenset({
    "mcp", "config", "doctor", "update", "upgrade", "install", "agents",
    "stop", "attach", "daemon", "auth", "plugin", "plugins", "setup-token",
    "migrate-installer",
})


def _real_claude() -> str:
    """The full path of the real claude, or the bare name as a last resort.

    ``claude_path`` from ``aipager.yaml`` first: the resolver runs
    ``claude --version`` on each candidate, seconds on every start."""
    from aipager import claude_resolve, scope
    pinned = scope.load_claude_path(scope.CONFIG_PATH)
    if (pinned and os.access(pinned, os.X_OK)
            and os.path.basename(pinned) != "aipager-claude"):
        return pinned
    resolved = claude_resolve.try_resolve_claude_binary()
    return resolved.chosen.path if resolved else "claude"


def _daemon_running() -> bool:
    from aipager.status import _daemon_alive
    return _daemon_alive()


def _live_labels() -> set[str]:
    from aipager.status import _live_sessions
    return {n.removeprefix("claude-") for n in _live_sessions()}


def _isatty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _aipager_bin() -> str:
    sibling = Path(sys.argv[0]).resolve().parent / "aipager"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("aipager") or "aipager"


def _passes_through(args: list[str]) -> bool:
    if os.environ.get(SESSION_ENV):
        return True
    if not _isatty():
        return True
    if _PASS_FLAGS & set(args):
        return True
    return bool(args) and args[0] in _SUBCOMMANDS


def session_name(cwd: str, taken: set[str]) -> str:
    """A free session name from the folder name: ``[a-z0-9_]``, not reserved,
    with the first free ``_N`` suffix when a live session has the name."""
    from aipager.dtach.inject import _RESERVED, normalize_session_name
    base = re.sub(r"[^a-z0-9_]", "", normalize_session_name(os.path.basename(cwd)))
    if not base or base in _RESERVED:
        base = "c"
    name, n = base, 0
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name


def main() -> None:
    args = sys.argv[1:]
    if _passes_through(args):
        _exec_real(args)
        return
    if not _daemon_running():
        print("aipager is not running: plain claude", file=sys.stderr)
        _exec_real(args)
        return
    name = session_name(os.getcwd(), _live_labels())
    exe = _aipager_bin()
    os.execvp(exe, [exe, "session", name, "--", *args])


def _exec_real(args: list[str]) -> None:
    claude = _real_claude()
    os.execvp(claude, [claude, *args])


# --- aipager wrap install | uninstall | status ---------------------------

def _read() -> str:
    try:
        return BASHRC_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _installed(text: str) -> bool:
    return BLOCK_START in text and BLOCK_END in text


def _write(text: str) -> None:
    """Atomically replace the bashrc, keeping its mode (through a symlink)."""
    target = Path(os.path.realpath(BASHRC_PATH))
    check_write(target)
    try:
        mode = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    tmp = target.with_name(target.name + ".aipager-tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _strip(text: str) -> str:
    """*text* without the marked block (and the blank line before it)."""
    pattern = re.compile(
        r"\n?" + re.escape(BLOCK_START) + r"\n.*?" + re.escape(BLOCK_END) + r"\n?",
        re.DOTALL)
    return pattern.sub("", text, count=1)


def cmd_install(args: argparse.Namespace) -> int:
    text = _read()
    if _installed(text):
        print(f"Already installed in {BASHRC_PATH}.")
        return 0
    sep = "" if not text or text.endswith("\n") else "\n"
    lead = "\n" if text else ""
    _write(text + sep + lead + BLOCK)
    print(f"Installed in {BASHRC_PATH}. Open a new shell: `claude` now runs under aipager.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    text = _read()
    if not _installed(text):
        print(f"Not installed in {BASHRC_PATH}.")
        return 0
    _write(_strip(text))
    print(f"Removed from {BASHRC_PATH}.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    on = _installed(_read())
    print(f"{BASHRC_PATH}: {'installed' if on else 'not installed'}")
    return 0 if on else 1


def cmd_wrap(args: argparse.Namespace) -> int:
    handlers = {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}
    fn = handlers.get(getattr(args, "wrap_cmd", None))
    return fn(args) if fn else 0


def add_parser(sub) -> argparse.ArgumentParser:
    wrap_p = sub.add_parser(
        "wrap", help="run plain `claude` under aipager (install/uninstall/status)")
    wrap_p.set_defaults(fn=cmd_wrap)
    wrap_sub = wrap_p.add_subparsers(dest="wrap_cmd")
    wrap_sub.add_parser("install", help="add the `claude` alias to ~/.bashrc")
    wrap_sub.add_parser("uninstall", help="remove the alias block from ~/.bashrc")
    wrap_sub.add_parser("status", help="show whether the alias is installed")
    return wrap_p

"""Two faults seen with ``/resume`` of a terminal (tmux) claude.

1. A ``claude -p`` started inside a session (a test, a script, a tool)
   inherits ``CLAUDE_DTACH_SESSION``. Its hooks used to count as the
   session's own: its ``UserPromptSubmit`` (no Telegram marker) rewrote the
   session's rights to the floor, so the session's next ``Bash`` was halted
   ("Bash is in this role's deny_tools"), and its ``Stop`` posted its JSON
   answer as the session's "Done" card. ``notify_hook`` now tells it apart
   by its parent's start environment and treats it as a foreign worker.
2. A second adopt of one conversation while the first waits for the
   terminal claude to exit ran the conversation twice (``name`` and
   ``name_2``).

No real process, socket, Telegram or claude: ``/proc`` is a tmp_path tree.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aipager import claude_catalog, claude_jobs
from aipager.bot import session_ops
from aipager.bot.session_ops import ResumeOutcome
from aipager.dtach import notify_hook
from aipager.state import SessionRegistry

SESSION = "claude-ff__d1234567"
OWN, PARENT = 4100, 4099


def _proc(root: Path, pid: int, ppid: int, env: dict[str, str]) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    # comm with a space and a ")" — the ppid is read after the last ")".
    (d / "stat").write_bytes(f"{pid} (my (odd) proc) S {ppid} 1 1 0".encode())
    (d / "environ").write_bytes(
        b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


@pytest.fixture
def proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(notify_hook, "_PROC", str(root))
    monkeypatch.setenv("CLAUDE_PID", str(OWN))
    return root


# ===== 1. a claude started inside the session ============================

def test_a_claude_whose_parent_holds_the_tag_is_nested(proc):
    _proc(proc, OWN, PARENT, {"CLAUDE_DTACH_SESSION": SESSION})
    _proc(proc, PARENT, 1, {"PATH": "/bin", "CLAUDE_DTACH_SESSION": SESSION})
    assert notify_hook._nested_claude(SESSION) is True


def test_the_sessions_own_claude_is_not_nested(proc):
    # Its parent is the launching ``bash -c``: the tag is only exported.
    _proc(proc, OWN, PARENT, {"CLAUDE_DTACH_SESSION": SESSION})
    _proc(proc, PARENT, 1, {"PATH": "/bin"})
    assert notify_hook._nested_claude(SESSION) is False


def test_another_sessions_tag_on_the_parent_is_not_nested(proc):
    _proc(proc, OWN, PARENT, {"CLAUDE_DTACH_SESSION": SESSION})
    _proc(proc, PARENT, 1, {"CLAUDE_DTACH_SESSION": "claude-other__d1"})
    assert notify_hook._nested_claude(SESSION) is False


@pytest.mark.parametrize("pid", ["", "abc", "12 3"])
def test_no_usable_claude_pid_is_not_nested(proc, monkeypatch, pid):
    _proc(proc, OWN, PARENT, {})
    _proc(proc, PARENT, 1, {"CLAUDE_DTACH_SESSION": SESSION})
    monkeypatch.setenv("CLAUDE_PID", pid)
    assert notify_hook._nested_claude(SESSION) is False


def test_an_unreadable_process_is_not_nested(proc):
    assert notify_hook._nested_claude(SESSION) is False


@pytest.fixture
def hook_main(monkeypatch):
    """``notify_hook.main`` with its process-wide effects stubbed: no
    memory cap on the test process, no socket, ``_run`` recorded."""
    calls = []
    monkeypatch.setenv("CLAUDE_DTACH_SESSION", SESSION)
    monkeypatch.setattr(notify_hook.resource, "setrlimit", lambda *a: None)
    monkeypatch.setattr(notify_hook, "_prepare_cap_notifier",
                        lambda *a: (None, b""))
    monkeypatch.setattr(notify_hook, "_resolve_worker", lambda s: None)
    monkeypatch.setattr(notify_hook, "_run",
                        lambda session, cap_slot, worker=None:
                        calls.append((session, worker)))
    return calls


def test_a_nested_claudes_hook_runs_as_a_foreign_worker(hook_main, monkeypatch):
    monkeypatch.setattr(notify_hook, "_nested_claude", lambda s: True)
    notify_hook.main()
    assert hook_main == [(SESSION, {"role": "foreign", "job": "",
                                    "session": SESSION})]


def test_the_sessions_own_claude_runs_as_itself(hook_main, monkeypatch):
    monkeypatch.setattr(notify_hook, "_nested_claude", lambda s: False)
    notify_hook.main()
    assert hook_main == [(SESSION, None)]


def test_a_foreign_prompt_sends_and_writes_nothing(monkeypatch):
    # What "foreign" buys: a UserPromptSubmit neither reaches the daemon
    # nor rewrites the session's rights.
    import io
    sent = []
    monkeypatch.setattr(notify_hook.sys, "stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": "route this",
         "session_id": "nested"})))
    monkeypatch.setattr(notify_hook, "_run_foreign",
                        lambda s, d, c: sent.append(d["hook_event_name"]))
    notify_hook._run(SESSION, [b"", False, False],
                     {"role": "foreign", "job": "", "session": SESSION})
    assert sent == ["UserPromptSubmit"]


# ===== 2. two adopts of one conversation =================================

SID = "0b6f3c1e-2d4a-4f8e-9c1a-7e5d3b2a1f00"
OWNER = 256113222  # the pinned personal-mode CHAT_ID (conftest)


def test_a_second_adopt_during_the_takeover_runs_nothing(
        mk_bot, run_async, tmp_path, monkeypatch):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(json.dumps({"type": "user", "cwd": "/work/My-Proj"}) + "\n")
    monkeypatch.setattr(claude_jobs, "find_transcript",
                        lambda sid, **kw: path if sid == SID else None)
    h = {"pid": 4242, "status": "idle", "name": ""}
    holders = [h, dict(h)]          # the first adopt's two looks
    alive = {4242}
    monkeypatch.setattr(claude_catalog, "holder",
                        lambda sid: holders.pop(0) if holders else None)
    monkeypatch.setattr(claude_jobs, "_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(session_ops, "_kill", lambda pid, sig: None)

    async def _sleep(_s):
        # The terminal claude exits while the first adopt waits: the
        # second tap runs in this gap.
        await asyncio.sleep(0)
        alive.discard(4242)

    monkeypatch.setattr(session_ops, "_sleep", _sleep)
    bot = mk_bot(SessionRegistry())
    bot._maybe_update_bot_name = AsyncMock()
    bot._update_bot_commands = AsyncMock()
    bot._do_resume_core = AsyncMock(
        return_value=ResumeOutcome(ok=True, reason="resumed"))
    bot._adopt_hooks_arrive = AsyncMock(return_value=True)

    async def _both():
        return await asyncio.gather(
            bot.adopt_conversation(SID, chat_id=OWNER, user_id=OWNER,
                                   confirm=True),
            bot.adopt_conversation(SID, chat_id=OWNER, user_id=OWNER,
                                   confirm=True))

    first, second = run_async(_both())
    assert first.ok and first.label == "my_proj"
    assert (second.ok, second.err) == (False, "already tracked")
    assert [s.label for s in bot.registry.all_sessions().values()] == ["my_proj"]
    bot._do_resume_core.assert_awaited_once()
    # The guard is released: a later adopt is judged afresh.
    assert SID not in bot.__dict__["_adopting"]

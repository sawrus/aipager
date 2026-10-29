"""Adopting a Claude Code conversation started without aipager
(``SessionOpsMixin.adopt_conversation``, fork; see
``aipager.claude_catalog``).

The catalog's ``holder`` is patched, the transcript lives under tmp_path,
the takeover's SIGTERM goes through the ``session_ops._kill`` seam and its
wait through ``session_ops._sleep``. No real Telegram, signal, dtach or
claude.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from aipager import claude_catalog, claude_jobs, policy
from aipager.bot import session_ops
from aipager.bot.session_ops import ResumeOutcome
from aipager.dtach import inject
from aipager.scope import Member, Scope
from aipager.state import SessionRegistry, Status

SID = "0b6f3c1e-2d4a-4f8e-9c1a-7e5d3b2a1f00"
OWNER = 256113222  # the pinned personal-mode CHAT_ID (conftest)
GROUP = -4152307515
ALY, ADA, BOB = 111, 222, 333


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """A transcript for SID whose first record with a cwd says
    ``/work/My-Proj``; ``find_transcript`` finds only it."""
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "summary", "summary": "x"}),
        "not json",
        json.dumps({"type": "user", "cwd": "/work/My-Proj"}),
        json.dumps({"type": "user", "cwd": "/elsewhere"}),
    ]) + "\n")
    monkeypatch.setattr(claude_jobs, "find_transcript",
                        lambda sid, **kw: path if sid == SID else None)
    return path


@pytest.fixture
def seams(monkeypatch):
    """The takeover's seams, recorded: kills, sleeps, and which pids are
    alive (``alive`` set; a SIGTERM removes its pid unless ``stubborn``)."""
    state = {"kills": [], "sleeps": 0, "alive": set(), "stubborn": False,
             "holders": []}

    def _kill(pid, sig):
        state["kills"].append((pid, sig))
        if not state["stubborn"]:
            state["alive"].discard(pid)

    async def _sleep(_s):
        state["sleeps"] += 1

    def _holder(sid):
        assert sid == SID
        return state["holders"].pop(0) if state["holders"] else None

    monkeypatch.setattr(session_ops, "_kill", _kill)
    monkeypatch.setattr(session_ops, "_sleep", _sleep)
    monkeypatch.setattr(claude_jobs, "_pid_alive",
                        lambda pid: pid in state["alive"])
    monkeypatch.setattr(claude_catalog, "holder", _holder)
    return state


def _bot(mk_bot, registry=None, *, scopes=None, outcome=None):
    bot = mk_bot(registry or SessionRegistry(), scopes=scopes)
    if scopes is not None:
        bot.policy = policy.load_policy(Path("/nonexistent/p.yaml"),
                                        Path("/nonexistent/p.d"))
    bot._maybe_update_bot_name = AsyncMock()
    bot._update_bot_commands = AsyncMock()
    bot._do_resume_core = AsyncMock(
        return_value=outcome or ResumeOutcome(ok=True, reason="resumed"))
    return bot


def _scopes():
    return [Scope(chat_id=GROUP, kind="group", label="team", members=(
                Member(id=ALY, label="aly", role="owner"),
                Member(id=ADA, label="ada", role="admin"),
                Member(id=BOB, label="bob", role="user")))]


def _adopt(bot, run_async, *, chat_id=OWNER, user_id=OWNER, confirm=False,
           sid=SID):
    return run_async(bot.adopt_conversation(sid, chat_id=chat_id,
                                            user_id=user_id, confirm=confirm))


# ===== not found / forbidden =============================================

def test_unknown_conversation_is_not_found(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    other = "11111111-2222-4333-8444-555555555555"
    out = _adopt(bot, run_async, sid=other)
    assert (out.ok, out.reason) == (False, "not_found")
    assert bot.registry.all_sessions() == {}
    bot._do_resume_core.assert_not_called()


def test_invalid_session_id_is_not_found(mk_bot, run_async, transcript, seams,
                                        monkeypatch):
    bot = _bot(mk_bot)
    # A lookup that would find anything: only the id check refuses.
    monkeypatch.setattr(claude_jobs, "find_transcript",
                        lambda sid, **kw: transcript)
    out = _adopt(bot, run_async, sid="../../etc/passwd")
    assert out.reason == "not_found"
    bot._do_resume_core.assert_not_called()


def test_a_plain_member_is_forbidden(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot, scopes=_scopes())
    seams["holders"] = [{"pid": 4242, "status": "idle", "name": ""}]
    out = _adopt(bot, run_async, chat_id=GROUP, user_id=BOB, confirm=True)
    assert (out.ok, out.reason) == (False, "forbidden")
    assert seams["kills"] == []
    assert bot.registry.all_sessions() == {}
    bot._do_resume_core.assert_not_called()


@pytest.mark.parametrize("user_id", [ALY, ADA])
def test_owner_and_admin_of_the_chat_may_adopt(mk_bot, run_async, transcript,
                                               seams, user_id):
    bot = _bot(mk_bot, scopes=_scopes())
    out = _adopt(bot, run_async, chat_id=GROUP, user_id=user_id)
    assert out.ok and out.reason == "ok"


def test_personal_mode_only_the_operator(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    out = _adopt(bot, run_async, user_id=999)
    assert out.reason == "forbidden"
    out = _adopt(bot, run_async, user_id=None)
    assert out.reason == "forbidden"
    bot._do_resume_core.assert_not_called()


# ===== the terminal holder ==============================================

def test_a_busy_terminal_is_left_alone(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    seams["holders"] = [{"pid": 4242, "status": "busy", "name": ""}]
    seams["alive"].add(4242)
    out = _adopt(bot, run_async, confirm=True)
    assert (out.ok, out.reason, out.holder_status) == (False, "terminal_busy", "busy")
    assert seams["kills"] == []
    assert bot.registry.all_sessions() == {}
    bot._do_resume_core.assert_not_called()


def test_an_idle_terminal_needs_a_takeover_first(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    seams["holders"] = [{"pid": 4242, "status": "idle", "name": ""}]
    seams["alive"].add(4242)
    out = _adopt(bot, run_async)
    assert (out.ok, out.reason, out.holder_status) == (False, "needs_takeover", "idle")
    assert seams["kills"] == []
    assert bot.registry.all_sessions() == {}
    bot._do_resume_core.assert_not_called()


def test_confirmed_takeover_sigterms_the_holder_then_adopts(
        mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    h = {"pid": 4242, "status": "waiting", "name": ""}
    seams["holders"] = [h, dict(h)]
    seams["alive"].add(4242)
    out = _adopt(bot, run_async, confirm=True)
    assert seams["kills"] == [(4242, signal.SIGTERM)]
    assert (out.ok, out.reason, out.label) == (True, "ok", "my_proj")
    bot._do_resume_core.assert_awaited_once()


def test_a_holder_that_does_not_exit_fails_the_takeover(
        mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    h = {"pid": 4242, "status": "idle", "name": ""}
    seams["holders"] = [h, dict(h)]
    seams["alive"].add(4242)
    seams["stubborn"] = True
    out = _adopt(bot, run_async, confirm=True)
    assert (out.ok, out.reason) == (False, "takeover_failed")
    assert out.err
    # SIGTERM only, once; the wait is bounded by the poll count.
    assert seams["kills"] == [(4242, signal.SIGTERM)]
    assert seams["sleeps"] == int(session_ops._TAKEOVER_WAIT_SECONDS
                                  / session_ops._TAKEOVER_POLL)
    assert bot.registry.all_sessions() == {}
    bot._do_resume_core.assert_not_called()


def test_a_holder_that_changed_is_not_killed(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    seams["holders"] = [{"pid": 4242, "status": "idle", "name": ""},
                        {"pid": 5353, "status": "idle", "name": ""}]
    seams["alive"].update({4242, 5353})
    out = _adopt(bot, run_async, confirm=True)
    assert seams["kills"] == []
    assert (out.ok, out.reason) == (False, "needs_takeover")
    assert bot.registry.all_sessions() == {}
    bot._do_resume_core.assert_not_called()


def test_a_holder_that_turned_busy_is_not_killed(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    seams["holders"] = [{"pid": 4242, "status": "idle", "name": ""},
                        {"pid": 4242, "status": "busy", "name": ""}]
    seams["alive"].add(4242)
    out = _adopt(bot, run_async, confirm=True)
    assert seams["kills"] == []
    assert (out.reason, out.holder_status) == ("terminal_busy", "busy")
    bot._do_resume_core.assert_not_called()


def test_a_holder_gone_before_the_kill_is_not_killed(
        mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    seams["holders"] = [{"pid": 4242, "status": "idle", "name": ""}, None]
    out = _adopt(bot, run_async, confirm=True)
    assert seams["kills"] == []
    assert out.ok and out.reason == "ok"


# ===== the label ========================================================

def test_label_from_the_folder_is_normalised(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    out = _adopt(bot, run_async)
    assert out.label == "my_proj"
    sess = bot.registry.find_by_label("my_proj", OWNER, include_gone=True)
    assert sess is not None
    assert sess.cwd == "/work/My-Proj"
    assert sess.scope_chat_id == OWNER


def test_label_prefers_the_holders_name(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    h = {"pid": 4242, "status": "idle", "name": "Fix-Login.Bug"}
    seams["holders"] = [h, dict(h)]
    seams["alive"].add(4242)
    out = _adopt(bot, run_async, confirm=True)
    assert out.label == "fix_loginbug"


def test_label_collision_gets_a_suffix(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    assert _adopt(bot, run_async).label == "my_proj"
    assert _adopt(bot, run_async).label == "my_proj_2"
    assert _adopt(bot, run_async).label == "my_proj_3"


def test_label_skips_a_name_another_entry_holds(mk_bot, run_async, transcript,
                                               seams):
    """A renamed session keeps its internal name: that name is taken even
    though no label in the chat says ``my_proj``."""
    registry = SessionRegistry()
    old = registry.get_or_create(f"claude-my_proj__d{OWNER}")
    old.label = "renamed"
    old.scope_chat_id = OWNER
    bot = _bot(mk_bot, registry)
    out = _adopt(bot, run_async)
    assert out.label == "my_proj_2"
    assert registry.get(f"claude-my_proj__d{OWNER}") is old


def test_label_skips_a_label_in_use_under_another_name(mk_bot, run_async,
                                                       transcript, seams):
    """A session from before chats had their own names (``claude-my_proj``)
    still owns its label in the chat."""
    registry = SessionRegistry()
    old = registry.get_or_create("claude-my_proj")
    old.label = "my_proj"
    old.scope_chat_id = OWNER
    bot = _bot(mk_bot, registry)
    assert _adopt(bot, run_async).label == "my_proj_2"


@pytest.mark.parametrize("pid", [0, 1, None, True])
def test_a_bad_holder_pid_is_never_signalled(mk_bot, run_async, transcript,
                                             seams, pid):
    bot = _bot(mk_bot)
    h = {"pid": pid, "status": "idle", "name": ""}
    seams["holders"] = [h, dict(h)]
    out = _adopt(bot, run_async, confirm=True)
    assert seams["kills"] == []
    assert out.reason == "takeover_failed"
    bot._do_resume_core.assert_not_called()


def test_label_never_a_reserved_command(mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    transcript.write_text(json.dumps({"cwd": "/srv/status"}) + "\n")
    out = _adopt(bot, run_async)
    assert out.label == "status_2"


def test_label_never_empty_and_never_too_long():
    taken = set()
    assert session_ops._adopt_label("", taken.__contains__) == "c1"
    assert session_ops._adopt_label("ÿ--.", taken.__contains__) == "c1"
    long = "a" * 50
    first = session_ops._adopt_label(long, taken.__contains__)
    assert first == "a" * 32
    taken.add(first)
    second = session_ops._adopt_label(long, taken.__contains__)
    assert second == "a" * 30 + "_2"
    assert inject._VALID_NAME.match(second)


def test_transcript_cwd_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    lines = [json.dumps({"type": "x"})] * 5 + [json.dumps({"cwd": "/late"})]
    path.write_text("\n".join(lines) + "\n")
    assert session_ops._transcript_cwd(path) == "/late"
    monkeypatch.setattr(session_ops, "_CWD_READ_LINES", 5)
    assert session_ops._transcript_cwd(path) == ""
    assert session_ops._transcript_cwd(tmp_path / "missing") == ""


# ===== the resume ======================================================

def test_ok_registers_a_gone_session_and_resumes_it_with_the_driver(
        mk_bot, run_async, transcript, seams):
    bot = _bot(mk_bot)
    seen = {}

    async def _core(sess, **kw):
        seen["status"] = sess.status
        seen["claude_session_id"] = sess.claude_session_id
        seen["kw"] = kw
        seen["in_registry"] = bot.registry.get(sess.name) is sess
        return ResumeOutcome(ok=True, reason="resumed")

    bot._do_resume_core = _core
    out = _adopt(bot, run_async)
    assert out.ok and out.label == "my_proj"
    assert seen == {"status": Status.GONE, "claude_session_id": SID,
                    "kw": {"driver_user_id": OWNER}, "in_registry": True}


@pytest.mark.parametrize("outcome", [
    ResumeOutcome(ok=False, reason="launch_failed", err="socket in use"),
    ResumeOutcome(ok=False, reason="bg_busy"),
])
def test_a_failed_resume_leaves_no_entry(mk_bot, run_async, transcript, seams,
                                         outcome):
    bot = _bot(mk_bot, outcome=outcome)
    out = _adopt(bot, run_async)
    assert (out.ok, out.reason) == (False, "launch_failed")
    assert out.err == (outcome.err or outcome.reason)
    assert bot.registry.all_sessions() == {}


def test_through_the_real_resume_core(mk_bot, run_async, transcript, seams,
                                      monkeypatch):
    """The registered entry is one ``_do_resume_core`` accepts: it
    launches ``claude --resume SID`` in the transcript's folder and the
    session comes up IDLE, driven by the adopter."""
    bot = _bot(mk_bot)
    del bot._do_resume_core  # the real one
    bot._resume_target = AsyncMock(return_value=(SID, None))
    bot._stale_move = AsyncMock(return_value="")
    bot._session_system_prompt = lambda *a, **kw: ""
    launches = []

    async def _launch(name, **kw):
        launches.append((name, kw))
        return True, ""

    monkeypatch.setattr(inject, "launch_session", _launch)
    out = _adopt(bot, run_async)
    assert out.ok and out.label == "my_proj"
    (name, kw), = launches
    assert name == f"my_proj__d{OWNER}"
    assert kw["resume_id"] == SID and kw["cwd"] == "/work/My-Proj"
    sess = bot.registry.find_by_label("my_proj", OWNER)
    assert sess.status == Status.IDLE
    assert sess.last_driver_user_id == OWNER

"""/resume → Other Claude sessions and `aipager resume --claude` (fork).

A person runs ``claude`` in a plain terminal; aipager never saw it. The
Telegram picker (owner and admin only) lists such conversations
(``claude_catalog.list_foreign``) and continues one through
``adopt_conversation``; the CLI does the same with ``holder`` and the
launcher. The catalog and the adopt flow are stubbed here: no real
``~/.claude``, no signal, no dtach.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from aipager import claude_catalog
from aipager.bot import dashboard, new_flow, tap_gate
from aipager.bot.tap_gate import PROMPT, VIEW
from aipager.claude_catalog import AdoptOutcome, ForeignConversation
from aipager.cli import resume as cli_resume_main
from aipager.cli import resume_foreign as cli_resume
from aipager.policy import load_policy
from aipager.scope import Member, Scope
from aipager.state import SessionRegistry, Status, TrackedSession

GROUP = -1001
ALY, BOB, RO, ADA = 1, 2, 3, 4      # owner, user, read_only, admin


def _conv(i=0, *, title=None, cwd=None, holder_pid=None, holder_status="",
          ago=120.0):
    sid = f"{i:08x}-1111-2222-3333-444455556666"
    return ForeignConversation(
        session_id=sid,
        cwd=cwd if cwd is not None else f"/srv/proj{i}",
        title=title if title is not None else f"task number {i}",
        last_ts=time.time() - ago,
        holder_pid=holder_pid, holder_status=holder_status)


@pytest.fixture
def listing(monkeypatch):
    """``list_foreign`` returns ``items`` (mutable); calls are counted."""
    state = {"items": [_conv(i) for i in range(3)], "calls": []}

    def _list(days=14, limit=200):
        state["calls"].append(days)
        return list(state["items"])

    monkeypatch.setattr(claude_catalog, "list_foreign", _list)
    return state


def _group_scope():
    return Scope(chat_id=GROUP, kind="group", label="team", members=(
        Member(id=ALY, label="aly", role="owner"),
        Member(id=BOB, label="bob", role="user"),
        Member(id=RO, label="ro", role="read_only"),
        Member(id=ADA, label="ada", role="admin"),
    ), root="/srv/team")


@pytest.fixture
def gbot(mk_bot):
    bot = mk_bot(scopes=[_group_scope()])
    bot.policy = load_policy(Path("/nonexistent/policy.yaml"),
                             Path("/nonexistent/policy.d"))
    return bot


def _tap(data, *, user_id=12345, chat_id=GROUP, msg_id=50):
    q = MagicMock()
    q.data = data
    q.from_user = MagicMock(id=user_id)
    q.message = MagicMock()
    q.message.chat = MagicMock(id=chat_id)
    q.message.chat_id = chat_id
    q.message.message_id = msg_id
    q.message.text = "card"
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    # A real CallbackQuery has neither: calling_chat_id / calling_user_id
    # then read message.chat and from_user.
    q.effective_chat = None
    q.effective_user = None
    u = MagicMock()
    u.callback_query = q
    u.effective_chat = q.message.chat
    u.effective_user = q.from_user
    u.message = None
    return u, q


def _answers(q):
    return [c.args[0] for c in q.answer.await_args_list if c.args]


def _edited(q):
    call = q.edit_message_text.await_args
    return call.args[0], call.kwargs.get("reply_markup")


def _datas(kb):
    return [b.callback_data for row in (kb.inline_keyboard if kb else ()) for b in row]


# ---- the /resume picker's button ----------------------------------------

def _has_foreign(kb):
    return "_:rcp:0" in _datas(kb)


@pytest.mark.parametrize("uid,shown", [(ALY, True), (ADA, True), (BOB, False)])
def test_the_button_is_only_for_the_owner_and_admins(gbot, mk_update, run_async,
                                                    uid, shown):
    update = mk_update("/resume", user_id=uid, chat_id=GROUP)
    run_async(gbot._handle_resume_cmd(update, MagicMock()))
    kb = update.message.reply_text.await_args.kwargs.get("reply_markup")
    assert _has_foreign(kb) is shown


def test_the_button_shows_when_nothing_of_aipagers_is_ended(mk_bot, mk_update, run_async):
    bot = mk_bot()        # personal mode: the operator is the admin
    update = mk_update("/resume")
    run_async(bot._handle_resume_cmd(update, MagicMock()))
    text = update.message.reply_text.await_args.args[0]
    kb = update.message.reply_text.await_args.kwargs.get("reply_markup")
    assert "No previous sessions" in text
    assert _datas(kb) == ["_:rcp:0"]


def test_the_picker_without_foreign_keeps_no_keyboard_when_empty(mk_bot):
    _text, kb = mk_bot()._render_resume_picker(0, None)
    assert kb is None


def test_the_button_sits_under_ended_sessions(mk_bot):
    reg = SessionRegistry()
    s = TrackedSession(name="claude-jim", label="jim", status=Status.GONE)
    s.claude_session_id, s.gone_at = "UUID", time.time() - 5
    reg._sessions[s.name] = s
    _text, kb = mk_bot(reg)._render_resume_picker(0, None, foreign=True)
    assert _datas(kb)[-1] == "_:rcp:0"
    _text, kb = mk_bot(reg)._render_resume_picker(0, None)
    assert "_:rcp:0" not in _datas(kb)


def test_paging_the_resume_picker_keeps_the_button_for_admins_only(gbot, run_async):
    for uid, shown in ((ALY, True), (BOB, False)):
        u, q = _tap("_:resume_page:0", user_id=uid)
        run_async(gbot._handle_callback(u, MagicMock()))
        assert _has_foreign(_edited(q)[1]) is shown


# ---- the list -----------------------------------------------------------

def test_the_list_renders_rows_markers_and_buttons(mk_bot, listing, monkeypatch):
    monkeypatch.setenv("HOME", "/home/aly")
    listing["items"] = [
        _conv(0, title="fix <the> login", cwd="/home/aly/work/app",
              holder_pid=77, holder_status="idle", ago=7200),
        _conv(1, title="", cwd="/srv/billing", holder_pid=78, holder_status="busy"),
        _conv(2, title="", cwd=""),
    ]
    text, kb = mk_bot()._render_foreign_picker(5, 0, refresh=True)
    assert "Other Claude Code conversations" in text
    assert "fix &lt;the&gt; login" in text
    assert "~/work/app" in text and "/home/aly" not in text
    assert "2h ago" in text
    assert "Open in a terminal and waiting." in text
    assert "Working in a terminal right now." in text
    # A row with no holder carries no marker: two markers for three rows.
    assert text.count("terminal") == 2
    labels = [b.text for row in kb.inline_keyboard for b in row]
    datas = _datas(kb)
    assert datas[:3] == [f"_:rc:{c.session_id[:8]}" for c in listing["items"]]
    assert labels[1].startswith("billing (")      # the folder's name
    assert labels[2].startswith("00000002 (")      # nothing else: the id
    assert datas[-1] == "_:resume_page:0"
    assert listing["calls"] == [14]


def test_a_long_title_is_trimmed_on_its_button_only(mk_bot, listing):
    long = "x" * 80
    listing["items"] = [_conv(0, title=long)]
    text, kb = mk_bot()._render_foreign_picker(5, 0, refresh=True)
    assert long in text
    label = kb.inline_keyboard[0][0].text
    assert long not in label and len(label) < 40


def test_an_empty_list_says_so_with_a_way_back(mk_bot, listing):
    listing["items"] = []
    text, kb = mk_bot()._render_foreign_picker(5, 0, refresh=True)
    assert "No other Claude Code conversations" in text
    assert _datas(kb) == ["_:resume_page:0"]


def test_the_list_pages_by_ten(mk_bot, listing):
    listing["items"] = [_conv(i) for i in range(23)]
    bot = mk_bot()
    _t, kb0 = bot._render_foreign_picker(5, 0, refresh=True)
    d0 = _datas(kb0)
    assert len([d for d in d0 if d.startswith("_:rc:")]) == 10
    assert "_:rcp:1" in d0 and not any(d == "_:rcp:-1" for d in d0)
    _t, kb2 = bot._render_foreign_picker(5, 2)
    d2 = _datas(kb2)
    assert [d for d in d2 if d.startswith("_:rc:")] == [
        f"_:rc:{c.session_id[:8]}" for c in listing["items"][20:]]
    assert "_:rcp:1" in d2 and "_:rcp:3" not in d2
    # Paging reads the cached listing: one call to the catalog.
    assert len(listing["calls"]) == 1


def test_every_callback_fits_telegrams_64_bytes(mk_bot, listing, monkeypatch):
    listing["items"] = [_conv(i, title="ж" * 80, cwd="/" + "d" * 200) for i in range(25)]
    bot = mk_bot()
    datas = []
    for page in range(3):
        datas += _datas(bot._render_foreign_picker(5, page, refresh=page == 0)[1])
    datas += _datas(bot._render_adopt_outcome(
        listing["items"][0], AdoptOutcome(False, "needs_takeover"), 5)[1])
    datas += _datas(bot._render_resume_picker(0, None, foreign=True)[1])
    assert datas
    for d in datas:
        assert len(d.encode()) <= 64, d


def test_the_cache_expires_after_ten_minutes(mk_bot, listing):
    bot = mk_bot()
    bot._render_foreign_picker(5, 0, refresh=True)
    conv = listing["items"][0]
    assert bot._foreign_lookup(5, conv.session_id[:8]) == conv
    assert bot._foreign_lookup(6, conv.session_id[:8]) is None    # per chat
    ts, items = bot._foreign_cache()[5]
    bot._foreign_cache()[5] = (ts - 601, items)
    assert bot._foreign_lookup(5, conv.session_id[:8]) is None
    bot._render_foreign_picker(5, 1)          # too old: listed afresh
    assert len(listing["calls"]) == 2


def test_an_ambiguous_short_id_is_unknown(mk_bot, listing):
    a, b = _conv(1), _conv(1)
    b = ForeignConversation(session_id=a.session_id[:8] + "-ffff", cwd="", title="t",
                            last_ts=a.last_ts)
    listing["items"] = [a, b]
    bot = mk_bot()
    bot._render_foreign_picker(5, 0, refresh=True)
    assert bot._foreign_lookup(5, a.session_id[:8]) is None
    assert bot._foreign_lookup(5, "") is None


# ---- taps ---------------------------------------------------------------

def test_opening_the_list_lists_afresh(mk_bot, listing, run_async):
    bot = mk_bot()
    for _ in range(2):
        u, q = _tap("_:rcp:0")
        run_async(bot._handle_callback(u, MagicMock()))
    assert len(listing["calls"]) == 2
    text, kb = _edited(q)
    assert "task number 0" in text
    u, q = _tap("_:rcp:1")
    run_async(bot._handle_callback(u, MagicMock()))
    assert len(listing["calls"]) == 2


@pytest.mark.parametrize("data", ["_:rcp:0", "_:rc:00000000", "_:rcy:00000000", "_:rcn"])
def test_a_member_who_is_no_admin_gets_nothing(gbot, listing, run_async, data):
    gbot.adopt_conversation = AsyncMock()
    gbot._render_foreign_picker(GROUP, 0, refresh=True)
    u, q = _tap(data, user_id=BOB)
    run_async(gbot._handle_callback(u, MagicMock()))
    assert _answers(q) == ["Only the chat's owner or an admin can do that."]
    q.edit_message_text.assert_not_awaited()
    gbot.adopt_conversation.assert_not_awaited()


@pytest.mark.parametrize("data", ["_:rc:deadbeef", "_:rcy:deadbeef"])
def test_an_unknown_id_answers_and_lists_afresh(mk_bot, listing, run_async, data):
    bot = mk_bot()
    bot.adopt_conversation = AsyncMock()
    u, q = _tap(data)
    run_async(bot._handle_callback(u, MagicMock()))
    assert _answers(q) == ["This list is out of date, so here it is again."]
    text, _kb = _edited(q)
    assert "Other Claude Code conversations" in text
    assert listing["calls"] == [14]
    bot.adopt_conversation.assert_not_awaited()


def test_a_stale_listing_answers_and_lists_afresh(mk_bot, listing, run_async):
    bot = mk_bot()
    bot.adopt_conversation = AsyncMock()
    chat = GROUP
    bot._render_foreign_picker(chat, 0, refresh=True)
    ts, items = bot._foreign_cache()[chat]
    bot._foreign_cache()[chat] = (ts - 601, items)
    u, q = _tap(f"_:rc:{items[0].session_id[:8]}", chat_id=chat)
    run_async(bot._handle_callback(u, MagicMock()))
    assert _answers(q) == ["This list is out of date, so here it is again."]
    bot.adopt_conversation.assert_not_awaited()


def _adopt_bot(mk_bot, listing, outcome):
    bot = mk_bot()
    bot.adopt_conversation = AsyncMock(return_value=outcome)
    bot._render_foreign_picker(GROUP, 0, refresh=True)
    return bot, listing["items"][0]


def test_selecting_asks_without_confirm(mk_bot, listing, run_async):
    bot, conv = _adopt_bot(mk_bot, listing, AdoptOutcome(False, "not_found"))
    u, q = _tap(f"_:rc:{conv.session_id[:8]}", user_id=77)
    run_async(bot._handle_callback(u, MagicMock()))
    bot.adopt_conversation.assert_awaited_once_with(
        conv.session_id, chat_id=GROUP, user_id=77, confirm=False)


def test_take_over_confirms(mk_bot, listing, run_async):
    bot, conv = _adopt_bot(mk_bot, listing, AdoptOutcome(False, "takeover_failed"))
    u, q = _tap(f"_:rcy:{conv.session_id[:8]}", user_id=77)
    run_async(bot._handle_callback(u, MagicMock()))
    bot.adopt_conversation.assert_awaited_once_with(
        conv.session_id, chat_id=GROUP, user_id=77, confirm=True)


def test_needs_takeover_draws_the_take_over_card(mk_bot, listing, run_async):
    bot, conv = _adopt_bot(mk_bot, listing,
                           AdoptOutcome(False, "needs_takeover", holder_status="idle"))
    u, q = _tap(f"_:rc:{conv.session_id[:8]}")
    run_async(bot._handle_callback(u, MagicMock()))
    text, kb = _edited(q)
    assert "is open in a terminal: close it there and continue here?" in text
    assert "task number 0" in text
    assert _datas(kb) == [f"_:rcy:{conv.session_id[:8]}", "_:rcn"]


def test_cancel_shows_the_list_again(mk_bot, listing, run_async):
    bot, conv = _adopt_bot(mk_bot, listing, AdoptOutcome(False, "needs_takeover"))
    u, q = _tap("_:rcn")
    run_async(bot._handle_callback(u, MagicMock()))
    text, _kb = _edited(q)
    assert "Other Claude Code conversations" in text
    bot.adopt_conversation.assert_not_awaited()
    assert len(listing["calls"]) == 1          # the cached list


@pytest.mark.parametrize("outcome,expect", [
    (AdoptOutcome(False, "terminal_busy", holder_status="busy"),
     "is working in the terminal right now, so try again when it waits."),
    (AdoptOutcome(False, "takeover_failed", err="still alive"),
     "The terminal Claude did not close, so nothing changed."),
    (AdoptOutcome(False, "forbidden"),
     "Only the chat's owner or an admin can continue these conversations."),
    (AdoptOutcome(False, "not_found"), "That conversation is no longer there."),
    (AdoptOutcome(False, "launch_failed", err="dtach is sad"),
     "Couldn't continue <b>task number 0</b>: dtach is sad"),
])
def test_each_reason_has_its_message(mk_bot, listing, run_async, outcome, expect):
    bot, conv = _adopt_bot(mk_bot, listing, outcome)
    u, q = _tap(f"_:rc:{conv.session_id[:8]}")
    run_async(bot._handle_callback(u, MagicMock()))
    text, kb = _edited(q)
    assert expect in text
    assert kb is None


def test_ok_shows_the_resumed_session_card(mk_bot, listing, run_async, monkeypatch):
    bot, conv = _adopt_bot(mk_bot, listing, AdoptOutcome(True, "ok", label="proj0"))
    sess = TrackedSession(name="claude-proj0", label="proj0", status=Status.IDLE)
    bot.registry._sessions[sess.name] = sess
    seen = []
    monkeypatch.setattr(bot, "_build_session_dashboard",
                        lambda s: seen.append(s) or "<DASH>")
    u, q = _tap(f"_:rc:{conv.session_id[:8]}")
    run_async(bot._handle_callback(u, MagicMock()))
    text, kb = _edited(q)
    assert "Resumed <b>proj0</b>" in text
    assert "<DASH>" in text and seen == [sess]
    assert kb is None


def test_every_adopt_reason_is_rendered(mk_bot, listing):
    """No reason of the contract falls to the unknown-reason default."""
    bot = mk_bot()
    conv = listing["items"][0]
    texts = {r: bot._render_adopt_outcome(conv, AdoptOutcome(r == "ok", r, label="p"),
                                          5)[0]
             for r in claude_catalog.ADOPT_REASONS}
    fallback = bot._render_adopt_outcome(conv, AdoptOutcome(False, "zz_new"), 5)[0]
    for reason, text in texts.items():
        if reason != "launch_failed":
            assert "Couldn't continue" not in text, reason
    assert "zz_new" in fallback


# ---- the tap gate and the /new card -------------------------------------

@pytest.mark.parametrize("verb,cap", [
    ("rcp:0", VIEW), ("rcp:3", VIEW), ("rc:0a1b2c3d", PROMPT),
    ("rcy:0a1b2c3d", PROMPT), ("rcn", PROMPT),
])
def test_tap_gate_classes(verb, cap):
    assert tap_gate.required_capability("_", verb) is cap


def test_only_the_list_leaves_the_new_card_open():
    def _leaves(data):
        u = MagicMock()
        u.message = None
        u.callback_query = MagicMock(data=data)
        return new_flow._leaves_the_card_open(MagicMock(), u)

    assert _leaves("_:rcp:0") and _leaves("_:rcp:2")
    for data in ("_:rc:0a1b2c3d", "_:rcy:0a1b2c3d", "_:rcn"):
        assert not _leaves(data), data


def test_the_reader_role_cannot_take_over(mk_bot, listing, run_async):
    """The gate stops a read_only member before the handler."""
    bot = mk_bot(scopes=[_group_scope()])
    bot.policy = load_policy(Path("/nonexistent/policy.yaml"),
                             Path("/nonexistent/policy.d"))
    bot.adopt_conversation = AsyncMock()
    u, q = _tap("_:rcy:00000000", user_id=RO)
    run_async(bot._handle_callback(u, MagicMock()))
    assert _answers(q) == [str(tap_gate.REFUSED_TEXT)]
    bot.adopt_conversation.assert_not_awaited()


def test_the_strings_are_whole_sentences_in_their_budgets():
    for msg in (dashboard.RESUME_FOREIGN_BTN, dashboard.RESUME_FOREIGN_TAKE_OVER,
                dashboard.RESUME_FOREIGN_CANCEL, dashboard.RESUME_FOREIGN_BACK):
        assert msg.key.startswith("btn.")
    for msg in (dashboard.RESUME_FOREIGN_STALE, dashboard.RESUME_FOREIGN_DENIED):
        assert msg.key.startswith("toast.")


# ---- the CLI ------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, listing):
    """Inputs fed in order; holder, signals, sleep and launch recorded."""
    state = {"inputs": [], "holder": [], "terminated": [], "alive": [],
             "launched": [], "slept": [], "prepare": []}

    def _input(prompt=""):
        state.setdefault("prompts", []).append(prompt)
        if not state["inputs"]:
            raise EOFError
        return state["inputs"].pop(0)

    def _holder(sid):
        return state["holder"].pop(0) if state["holder"] else None

    async def _launch(name, **kw):
        state["launched"].append((name, kw))
        return True, ""

    def _prepare(sid):
        state["prepare"].append(sid)
        return sid, 0

    clock = {"t": 0.0}

    def _sleep(s):
        state["slept"].append(s)
        clock["t"] += s

    monkeypatch.setattr("builtins.input", _input)
    monkeypatch.setattr(claude_catalog, "holder", _holder)
    monkeypatch.setattr(cli_resume, "_terminate", lambda pid: state["terminated"].append(pid))
    monkeypatch.setattr(cli_resume, "_pid_alive",
                        lambda pid: state["alive"].pop(0) if state["alive"] else False)
    monkeypatch.setattr(cli_resume, "_sleep", _sleep)
    monkeypatch.setattr(cli_resume, "_clock", lambda: clock["t"])
    monkeypatch.setattr("aipager.dtach.inject.launch_session", _launch)
    monkeypatch.setattr("aipager.dtach.launcher.prepare_resume", _prepare)
    monkeypatch.setattr("aipager.status._live_sessions", lambda: set())
    return state


def _run(args_name=None):
    return cli_resume_main._cmd_resume(argparse.Namespace(name=args_name, claude=True))


def test_cli_table_and_paging(cli, listing, capsys):
    listing["items"] = [_conv(i, holder_pid=9 if i == 1 else None,
                              holder_status="busy" if i == 1 else "") for i in range(12)]
    listing["items"][0] = _conv(0, holder_pid=8, holder_status="idle")
    cli["inputs"] = ["n", "p", "q"]
    assert _run() == 0
    out = capsys.readouterr().out
    assert "page 1/2 (12 total)" in out and "page 2/2" in out
    assert "task number 0" in out and "/srv/proj0" in out and "2m ago" in out
    assert "task number 11" in out
    assert "[terminal: waiting]" in out and "[terminal: working]" in out
    # Page 1 is drawn twice (before n and after p), page 2 has none.
    assert out.count("[terminal:") == 4
    assert cli["launched"] == []


def test_cli_with_a_name_is_refused(cli):
    assert _run("x1") == 2
    assert cli["launched"] == []


def test_cli_nothing_to_list(cli, listing, capsys):
    listing["items"] = []
    assert _run() == 0
    assert cli["launched"] == []


def test_cli_launch_args_without_a_holder(cli, listing):
    listing["items"] = [_conv(0, cwd="/srv/My-Project.v2")]
    cli["inputs"] = ["1"]
    assert _run() == 0
    assert cli["terminated"] == []
    (name, kw), = cli["launched"]
    assert name == "my_project_v2"
    assert kw["resume_id"] == listing["items"][0].session_id
    assert kw["cwd"] == "/srv/My-Project.v2"
    assert kw["mode"] == "auto"
    assert cli["prepare"] == [listing["items"][0].session_id]


def test_cli_label_is_not_reserved_and_unique(cli, listing, monkeypatch):
    monkeypatch.setattr("aipager.status._live_sessions",
                        lambda: {"claude-status_2", "claude-api"})
    assert cli_resume._foreign_label("/srv/status") == "status_3"
    assert cli_resume._foreign_label("/srv/api") == "api_2"
    assert cli_resume._foreign_label("/srv/web") == "web"
    assert cli_resume._foreign_label("") == "claude"
    assert cli_resume._foreign_label("/srv/@@@") == "claude"


def test_cli_waiting_holder_no_leaves_it(cli, listing):
    cli["holder"] = [{"pid": 41, "status": "idle", "name": ""}]
    cli["inputs"] = ["1", "n"]
    assert _run() == 0
    assert any("Close it in the terminal and continue under aipager? [y/N]" in p
               for p in cli["prompts"])
    assert cli["terminated"] == [] and cli["launched"] == []


def test_cli_waiting_holder_yes_closes_and_launches(cli, listing):
    held = {"pid": 41, "status": "idle", "name": ""}
    cli["holder"] = [held, held]
    cli["alive"] = [True, True, False]
    cli["inputs"] = ["1", "y"]
    assert _run() == 0
    assert cli["terminated"] == [41]
    assert len(cli["slept"]) == 2
    assert len(cli["launched"]) == 1


def test_cli_holder_that_will_not_exit_refuses(cli, listing):
    held = {"pid": 41, "status": "idle", "name": ""}
    cli["holder"] = [held, held]
    cli["alive"] = [True] * 1000
    cli["inputs"] = ["1", "y"]
    assert _run() == 1
    assert cli["terminated"] == [41]
    assert 10.0 <= sum(cli["slept"]) < 10.5
    assert cli["launched"] == []


def test_cli_busy_holder_refuses_without_asking(cli, listing):
    cli["holder"] = [{"pid": 41, "status": "busy", "name": ""}]
    cli["inputs"] = ["1", "y"]
    assert _run() == 1
    assert cli["inputs"] == ["y"]            # never asked
    assert cli["terminated"] == [] and cli["launched"] == []


def test_cli_holder_busy_by_the_time_of_yes_refuses(cli, listing):
    cli["holder"] = [{"pid": 41, "status": "idle", "name": ""},
                     {"pid": 41, "status": "busy", "name": ""}]
    cli["inputs"] = ["1", "y"]
    assert _run() == 1
    assert cli["terminated"] == [] and cli["launched"] == []


def test_cli_launch_failure_is_an_error(cli, listing, monkeypatch):
    async def _fail(name, **kw):
        return False, "dtach is sad"

    monkeypatch.setattr("aipager.dtach.inject.launch_session", _fail)
    cli["inputs"] = ["1"]
    assert _run() == 1


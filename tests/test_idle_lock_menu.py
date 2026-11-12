"""/settings -> 🔒 Idle lock (aipager/bot/idle_lock_menu.py) and
``aipager lock enable --after-min``: one time scheme, 1 h / 8 h / 16 h."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from aipager import idle_lock, scope
from aipager.bot import idle_lock_menu, settings_menu
from aipager.cli import lock as lock_cli
from aipager.policy import load_policy
from aipager.scope import Member, Scope

DM = 555
GROUP = -1001


def _config(enabled=True, after_min=60):
    data = {"schema_version": 3, "bot_token": "1:x", "scopes": [
        {"kind": "dm", "chat_id": DM, "label": "owner DM",
         "members": [{"id": 12345, "label": "owner", "role": "owner"}]}]}
    data["security"] = {"idle_lock": {"enabled": enabled, "after_min": after_min}}
    scope.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    scope.CONFIG_PATH.write_text(yaml.safe_dump(data), encoding="utf-8")
    idle_lock.LOCK.forget_cache()


def _buttons(kb):
    return [b for row in kb.to_dict()["inline_keyboard"] for b in row]


def _stored():
    return yaml.safe_load(scope.CONFIG_PATH.read_text())["security"]["idle_lock"]


@pytest.fixture
def mk_query():
    def _mk(callback_data, *, user_id=12345, chat_id=DM):
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.message = MagicMock()
        query.message.message_id = 42
        query.message.text = ""
        query.message.chat = MagicMock()
        query.message.chat.id = chat_id
        query.from_user = MagicMock()
        query.from_user.id = user_id
        query.from_user.username = "alice"
        update = MagicMock()
        update.callback_query = query
        update.effective_user = query.from_user
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        return update, query
    return _mk


# ---- the pages -----------------------------------------------------------------

def test_the_choices_are_one_eight_and_sixteen_hours():
    assert idle_lock.AFTER_MIN_CHOICES == (60, 480, 960)
    assert idle_lock_menu.CHOICES_MIN == (60, 480, 960)


@pytest.mark.parametrize("after,marked", [(60, "1 h ✅"), (480, "8 h ✅"), (960, "16 h ✅")])
def test_the_page_marks_the_current_choice(after, marked):
    _config(after_min=after)
    _text, kb = idle_lock_menu.render_page(DM)
    rows = _buttons(kb)
    assert [b["callback_data"] for b in rows] == [
        "_:set:il:60", "_:set:il:480", "_:set:il:960", "_:set:back"]
    assert [b["text"] for b in rows[:3]].count(marked) == 1
    assert sum("✅" in b["text"] for b in rows) == 1


def test_a_stored_time_outside_the_list_is_shown_custom_and_marks_nothing():
    _config(after_min=45)
    text, kb = idle_lock_menu.render_page(DM)
    assert "custom: 45 min" in text
    assert not any("✅" in b["text"] for b in _buttons(kb))
    root = [b["text"] for b in _buttons(settings_menu.render_settings_root(DM)[1])
            if b["callback_data"] == "_:set:il"]
    assert root == ["🔒 Idle lock: custom: 45 min"]


def test_the_root_row_shows_the_time_and_that_the_lock_is_off():
    _config(enabled=False, after_min=480)
    root = [b["text"] for b in _buttons(settings_menu.render_settings_root(DM)[1])
            if b["callback_data"] == "_:set:il"]
    assert root == ["🔒 Idle lock: 8 h, lock off"]
    assert "lock is off" in idle_lock_menu.render_page(DM)[0]


# ---- the taps ------------------------------------------------------------------

@pytest.mark.parametrize("after", [60, 480, 960])
def test_a_tap_writes_after_min_and_takes_effect_without_restart(
        mk_bot, mk_query, run_async, after):
    _config(enabled=True, after_min=45)
    assert idle_lock.LOCK.settings()["after_min"] == 45      # warms the cache
    update, query = mk_query(f"_:set:il:{after}")
    run_async(mk_bot()._handle_callback(update, MagicMock()))
    assert _stored() == {"enabled": True, "after_min": after}
    assert idle_lock.LOCK.settings()["after_min"] == after
    kb = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert sum("✅" in b["text"] for b in _buttons(kb)) == 1


def test_picking_a_time_keeps_a_disabled_lock_disabled(mk_bot, mk_query, run_async):
    _config(enabled=False)
    update, _query = mk_query("_:set:il:960")
    run_async(mk_bot()._handle_callback(update, MagicMock()))
    assert _stored() == {"enabled": False, "after_min": 960}


def test_the_page_opens_in_place(mk_bot, mk_query, run_async):
    _config()
    update, query = mk_query("_:set:il")
    run_async(mk_bot()._handle_callback(update, MagicMock()))
    assert "Idle lock" in query.edit_message_text.await_args.args[0]


@pytest.mark.parametrize("data", ["_:set:il:30", "_:set:il:abc", "_:set:il:60:x"])
def test_a_bad_tap_changes_nothing(mk_bot, mk_query, run_async, data):
    _config(after_min=480)
    update, query = mk_query(data)
    run_async(mk_bot()._handle_callback(update, MagicMock()))
    assert _stored()["after_min"] == 480
    query.edit_message_text.assert_not_awaited()


def test_a_member_cannot_change_the_time(mk_bot, mk_query, run_async):
    _config()
    bot = mk_bot(scopes=[Scope(chat_id=GROUP, kind="group", label="dev", members=(
        Member(id=1, label="owner", role="owner"), Member(id=2, label="bob", role="user")))])
    bot.policy = load_policy()
    update, query = mk_query("_:set:il:960", chat_id=GROUP, user_id=2)
    run_async(bot._handle_callback(update, MagicMock()))
    assert _stored()["after_min"] == 60
    query.edit_message_text.assert_not_awaited()
    toasts = [c.args[0] for c in query.answer.await_args_list if c.args and c.args[0]]
    assert any("admin" in t.lower() for t in toasts)


# ---- the command line ----------------------------------------------------------

@pytest.mark.parametrize("after", [60, 480, 960])
def test_cli_enable_takes_only_the_choices(after, capsys):
    _config(enabled=False)
    idle_lock.write_secret("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    assert lock_cli.cmd_enable(argparse.Namespace(after_min=after)) == 0
    assert _stored() == {"enabled": True, "after_min": after}


@pytest.mark.parametrize("after", [0, 1, 59, 61, 600, 1440])
def test_cli_enable_refuses_other_times_and_lists_the_choices(after, capsys):
    _config(enabled=False)
    idle_lock.write_secret("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    assert lock_cli.cmd_enable(argparse.Namespace(after_min=after)) == 2
    out = capsys.readouterr().out
    assert "60" in out and "480" in out and "960" in out
    assert _stored() == {"enabled": False, "after_min": 60}


def test_cli_enable_without_after_min_keeps_the_stored_time(capsys):
    _config(enabled=False, after_min=480)
    idle_lock.write_secret("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    assert lock_cli.cmd_enable(argparse.Namespace(after_min=None)) == 0
    assert _stored() == {"enabled": True, "after_min": 480}

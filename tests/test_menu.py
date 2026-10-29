"""aipager.bot.menu (fork): the main keyboard's menu rows and the chat
clean-up a menu tap does."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aipager import i18n, scope
from aipager.bot import menu

CHAT = 4242
#: The real switch: conftest turns the clean-up off for every other test.
REAL_ENABLED = menu.enabled


@pytest.fixture(autouse=True)
def _clean_up_on(monkeypatch):
    monkeypatch.setattr(menu, "enabled", REAL_ENABLED)


def _labels(code):
    return [i18n.get(code).text(msg) for _name, msg in menu.MENU]


def _tap(mk_update, text, *, message_id=3000, chat_type="private", chat_id=CHAT):
    update = mk_update(text, message_id=message_id, chat_id=chat_id, user_id=chat_id)
    update.message.chat_id = chat_id
    update.message.chat.type = chat_type
    return update


@pytest.fixture
def bot(mk_bot):
    b = mk_bot()
    b._app.bot.delete_messages = AsyncMock(return_value=True)
    b._app.bot.send_message = AsyncMock()
    for name in menu.HANDLERS.values():
        setattr(b, name, AsyncMock())
    return b


def _deleted(bot) -> list[int]:
    return [i for call in bot._app.bot.delete_messages.await_args_list for i in call.args[1]]


# ===== labels and rows ===================================================

@pytest.mark.parametrize("code", ["en", "ru", "fa"])
def test_every_label_resolves_to_its_action(code):
    for (name, _msg), label in zip(menu.MENU, _labels(code)):
        assert menu.action(label) == name, (code, label)


def test_other_text_is_no_menu_action():
    assert menu.action("resume") is None
    assert menu.action("📚 Resume please") is None
    assert menu.action("status") is None


def test_rows_hold_every_label_three_per_row():
    rows = menu.rows(i18n.get("en"), lambda label: label)
    assert [len(r) for r in rows] == [3, 3]
    assert [b for r in rows for b in r] == _labels("en")


def test_every_action_has_a_handler_and_now_and_stop_keep_the_chat():
    assert set(menu.HANDLERS) == {name for name, _ in menu.MENU}
    assert "now" not in menu.CLEANING and "stop" not in menu.CLEANING
    assert {"status", "new"} <= menu.CLEANING


# ===== what is cleaned ===================================================

def test_first_clean_looks_back_the_span_and_keeps_the_tap():
    ids = menu.to_clean(CHAT, 5000)
    assert ids == list(range(5000 - menu.CLEAN_SPAN, 5000))


def test_next_clean_starts_after_the_last_tap():
    menu.to_clean(CHAT, 5000)
    assert menu.to_clean(CHAT, 5010) == list(range(5001, 5010))
    # Another chat has its own mark.
    assert menu.to_clean(CHAT + 1, 20) == list(range(1, 20))


def test_kept_ids_are_left_out():
    assert menu.to_clean(CHAT, 10, keep={3, 7}) == [1, 2, 4, 5, 6, 8, 9]


def test_a_broken_state_file_starts_afresh():
    menu.MENU_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    menu.MENU_STATE_PATH.write_text(json.dumps({str(CHAT): "x"}))
    assert menu.to_clean(CHAT, 5) == [1, 2, 3, 4]


def test_open_prompts_and_busy_cards_are_kept():
    from types import SimpleNamespace
    sessions = {
        "a": SimpleNamespace(pending_prompt_msg={"msg_id": 11, "chat_id": CHAT},
                             extra_prompts=[{"msg_id": 12}, {"perm": {}}, "junk"],
                             busy_msg_id=13),
        "b": SimpleNamespace(pending_prompt_msg=None, extra_prompts=[], busy_msg_id=None),
    }
    fake = SimpleNamespace(registry=SimpleNamespace(all_sessions=lambda chat_id: sessions))
    assert menu.kept_messages(fake, CHAT) == {11, 12, 13}


def test_clean_chat_false_turns_it_off(tmp_path, monkeypatch):
    path = tmp_path / "aipager.yaml"
    monkeypatch.setattr(scope, "CONFIG_PATH", path)
    assert REAL_ENABLED() is True
    path.write_text("menu:\n  clean_chat: false\n")
    assert REAL_ENABLED() is False
    path.write_text("menu:\n  clean_chat: 0\n")
    assert REAL_ENABLED() is True


# ===== a tap through the router =========================================

def test_a_menu_tap_cleans_then_runs_the_bare_command(bot, mk_update, run_async):
    update = _tap(mk_update, i18n.get("en").text(menu.MENU[0][1]), message_id=3000)
    seen = []
    bot._handle_resume_cmd.side_effect = lambda u, c: seen.append(
        (u.message.text, bool(bot._app.bot.delete_messages.await_count)))
    run_async(bot._handle_message(update, MagicMock()))
    assert seen == [("/resume", True)]
    assert _deleted(bot) == list(range(3000 - menu.CLEAN_SPAN, 3000))
    assert all(len(c.args[1]) <= menu.DELETE_CHUNK
               for c in bot._app.bot.delete_messages.await_args_list)


def test_a_russian_mode_tap_runs_mode(bot, mk_update, run_async):
    update = _tap(mk_update, i18n.get("ru").text(dict(menu.MENU)["mode"]))
    run_async(bot._handle_message(update, MagicMock()))
    bot._handle_mode_cmd.assert_awaited_once()
    assert update.message.text == "/mode"


def test_now_runs_without_cleaning(bot, mk_update, run_async):
    update = _tap(mk_update, i18n.get("en").text(dict(menu.MENU)["now"]))
    run_async(bot._handle_message(update, MagicMock()))
    bot._handle_now_cmd.assert_awaited_once()
    bot._app.bot.delete_messages.assert_not_called()


def test_a_status_word_cleans_a_private_chat(bot, mk_update, run_async):
    bot._handle_status = AsyncMock()
    run_async(bot._handle_message(_tap(mk_update, "status", message_id=40), MagicMock()))
    bot._handle_status.assert_awaited_once()
    assert _deleted(bot) == list(range(1, 40))


def test_stop_keeps_the_chat(bot, mk_update, run_async):
    bot._handle_stop_cmd = AsyncMock()
    run_async(bot._handle_message(_tap(mk_update, "stop", message_id=40), MagicMock()))
    bot._handle_stop_cmd.assert_awaited_once()
    bot._app.bot.delete_messages.assert_not_called()


def test_off_in_config_runs_without_cleaning(bot, mk_update, run_async, monkeypatch):
    monkeypatch.setattr(menu, "enabled", lambda: False)
    update = _tap(mk_update, i18n.get("en").text(dict(menu.MENU)["help"]))
    run_async(bot._handle_message(update, MagicMock()))
    bot._handle_help_cmd.assert_awaited_once()
    bot._app.bot.delete_messages.assert_not_called()


def test_a_failed_delete_still_runs_the_command(bot, mk_update, run_async):
    bot._app.bot.delete_messages.side_effect = RuntimeError("too old")
    update = _tap(mk_update, i18n.get("en").text(dict(menu.MENU)["settings"]))
    run_async(bot._handle_message(update, MagicMock()))
    bot._handle_settings_cmd.assert_awaited_once()


def test_clean_needs_a_private_chat(bot, mk_update, run_async):
    update = _tap(mk_update, "x", chat_type="supergroup", chat_id=-100123)
    assert run_async(menu.clean(bot, update, "status")) == 0
    bot._app.bot.delete_messages.assert_not_called()


# ===== the keyboard =====================================================

def _keyboard_texts(bot) -> list[str]:
    markup = bot._app.bot.send_message.await_args.kwargs["reply_markup"]
    return [b.text for row in markup.keyboard for b in row]


def test_a_private_main_keyboard_has_every_menu_button(bot, run_async):
    run_async(bot._send_keyboard(level="main", chat_id=CHAT))
    texts = _keyboard_texts(bot)
    assert all(label in texts for label in _labels("en"))


def test_a_group_main_keyboard_has_no_menu_rows(bot, run_async):
    from aipager.bot import group_intake
    run_async(bot._send_keyboard(level="main", chat_id=-100123))
    texts = [group_intake.keyboard_tap(t)[0] for t in _keyboard_texts(bot)]
    assert any(t == "status" for t in texts)
    assert not any(menu.action(t) for t in texts)


def test_a_marked_group_tap_of_a_menu_label_runs_no_command(bot, mk_update, run_async):
    from aipager.bot import group_intake
    label = group_intake.mark_label(i18n.get("en").text(dict(menu.MENU)["help"]), -100123)
    update = _tap(mk_update, label, chat_type="supergroup", chat_id=-100123)
    run_async(bot._handle_message(update, MagicMock()))
    bot._handle_help_cmd.assert_not_called()
    bot._app.bot.delete_messages.assert_not_called()

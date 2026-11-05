"""Other chats see no command menu: no list without a scope, old lists cleared."""

from __future__ import annotations

from unittest.mock import AsyncMock

from telegram import (
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    BotCommandScopeDefault,
    MenuButtonDefault,
)

from aipager.i18n.locales import LOCALES, enabled_codes
from aipager.scope import Member, Scope

DM, GROUP = 100, -300


def _scopes():
    return [
        Scope(chat_id=DM, kind="dm", label="a",
              members=(Member(id=DM, label="a", role="owner"),)),
        Scope(chat_id=GROUP, kind="group", label="g",
              members=(Member(id=11, label="c", role="user"),
                       Member(id=12, label="d", role="user"))),
    ]


def _mock(bot):
    for name in ("set_my_commands", "delete_my_commands", "set_chat_menu_button"):
        setattr(bot._app.bot, name, AsyncMock())


def test_scope_mode_never_sets_commands_without_scope(mk_bot, run_async):
    bot = mk_bot(scopes=_scopes())
    _mock(bot)
    run_async(bot._update_bot_commands())
    scopes = [c.kwargs.get("scope") for c in bot._app.bot.set_my_commands.await_args_list]
    assert scopes and all(s is not None for s in scopes)
    assert not any(isinstance(s, BotCommandScopeDefault) for s in scopes)


def test_group_menu_goes_to_each_member_not_the_chat(mk_bot, run_async):
    bot = mk_bot(scopes=_scopes())
    _mock(bot)
    run_async(bot._update_bot_commands())
    scopes = [c.kwargs["scope"] for c in bot._app.bot.set_my_commands.await_args_list]
    members = {(s.chat_id, s.user_id) for s in scopes
               if isinstance(s, BotCommandScopeChatMember)}
    assert members == {(GROUP, 11), (GROUP, 12)}
    chats = [s.chat_id for s in scopes if isinstance(s, BotCommandScopeChat)]
    assert chats == [DM]


def test_legacy_mode_sets_commands_for_the_chat_id(mk_bot, run_async, monkeypatch):
    monkeypatch.setattr("aipager.config.CHAT_ID", 4242)
    bot = mk_bot()
    _mock(bot)
    run_async(bot._update_bot_commands())
    calls = bot._app.bot.set_my_commands.await_args_list
    assert len(calls) == 1
    scope = calls[0].kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == 4242


def test_clear_deletes_the_four_foreign_scopes_in_every_language(mk_bot, run_async):
    bot = mk_bot(scopes=_scopes())
    _mock(bot)
    run_async(bot._clear_foreign_menus())
    calls = bot._app.bot.delete_my_commands.await_args_list
    kinds = (BotCommandScopeDefault, BotCommandScopeAllPrivateChats,
             BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators)
    langs = {None} | {LOCALES[c].telegram_code for c in enabled_codes()
                      if LOCALES[c].telegram_code}
    for kind in kinds:
        got = {c.kwargs.get("language_code") for c in calls
               if isinstance(c.kwargs["scope"], kind)}
        assert got == langs


def test_clear_drops_the_group_wide_list_but_not_the_dm(mk_bot, run_async):
    bot = mk_bot(scopes=_scopes())
    _mock(bot)
    run_async(bot._clear_foreign_menus())
    chats = [c.kwargs["scope"].chat_id
             for c in bot._app.bot.delete_my_commands.await_args_list
             if isinstance(c.kwargs["scope"], BotCommandScopeChat)]
    assert chats == [GROUP]


def test_clear_resets_the_default_menu_button_without_chat_id(mk_bot, run_async):
    bot = mk_bot(scopes=_scopes())
    _mock(bot)
    run_async(bot._clear_foreign_menus())
    call = bot._app.bot.set_chat_menu_button.await_args
    assert "chat_id" not in call.kwargs
    assert isinstance(call.kwargs["menu_button"], MenuButtonDefault)


def test_clear_survives_api_errors(mk_bot, run_async):
    bot = mk_bot(scopes=_scopes())
    _mock(bot)
    bot._app.bot.delete_my_commands.side_effect = RuntimeError("boom")
    bot._app.bot.set_chat_menu_button.side_effect = RuntimeError("boom")
    run_async(bot._clear_foreign_menus())

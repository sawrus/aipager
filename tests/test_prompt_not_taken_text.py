"""Which notice a send with no hook gets: hooks never seen, a "/" send, or the
generic one. The slash flag is stamped by ``_inject_prompt``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aipager.state import Status, TrackedSession


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    monkeypatch.setattr("aipager.bot.notify.send_rich_message", AsyncMock(return_value={}))


def _notice(mk_bot, run_async, *, hook_at, slash):
    bot = mk_bot()
    bot._settle_card_text = AsyncMock(return_value=False)
    bot._app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    sess = TrackedSession(name="claude-jim", label="jim", status=Status.BUSY)
    sess.turn_hook_at = hook_at
    sess.prompt_sent_slash = slash
    run_async(bot.notify(sess, "prompt_not_taken", {"grace": 8.0}))
    return bot._app.bot.send_message.await_args.args[1] \
        if len(bot._app.bot.send_message.await_args.args) > 1 \
        else bot._app.bot.send_message.await_args.kwargs.get("text", "")


def test_no_hook_ever_names_disabled_hooks(mk_bot, run_async):
    text = _notice(mk_bot, run_async, hook_at=0.0, slash=True)
    assert "disableAllHooks" in text and "none since" in text
    assert '"/"' not in text


def test_slash_send_names_command(mk_bot, run_async):
    text = _notice(mk_bot, run_async, hook_at=5.0, slash=True)
    assert '"/"' in text and "disableAllHooks" not in text


def test_plain_send_is_generic_without_slash(mk_bot, run_async):
    text = _notice(mk_bot, run_async, hook_at=5.0, slash=False)
    assert "disableAllHooks" in text and '"/"' not in text and "none since" not in text
    assert "cannot tell" in text


@pytest.mark.parametrize("text,slash", [("/cost", True), ("hello", False)])
def test_inject_prompt_stamps_slash_flag(mk_bot, run_async, monkeypatch, tmp_path,
                                         text, slash):
    from aipager import policy_snapshot as ps
    from aipager.dtach import inject
    monkeypatch.setattr(ps, "snapshot_path", lambda n: tmp_path / f"{n}.json")
    monkeypatch.setattr(ps, "reply_context_path", lambda n: tmp_path / f"{n}.txt")
    monkeypatch.setattr(inject, "send_text_and_enter", AsyncMock(return_value=True))
    bot = mk_bot()
    sess = TrackedSession(name="claude-jim", label="jim", status=Status.IDLE)
    sess.prompt_sent_slash = not slash
    assert run_async(bot._inject_prompt(sess, text, msg_id=1, chat_id=2))
    assert sess.prompt_sent_slash is slash

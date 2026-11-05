"""The bot's description is cleared at start, never set.

Both the description and the short description go to an empty text for the
default and for every language of the project; an error on one call never
stops the rest; the start never waits for it. The last tests go through the
daemon's real bot to a fake HTTP transport: an empty text leaves as "".
"""

from __future__ import annotations

import asyncio
import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import httpx

from aipager.bot import bot_descriptions as bd
from aipager.i18n.locales import LOCALES, enabled_codes

TG_CODES = {LOCALES[c].telegram_code for c in enabled_codes() if LOCALES[c].telegram_code}


def _calls(tg, setter):
    return [c.kwargs for c in getattr(tg, setter).await_args_list]


def test_both_kinds_are_cleared_for_default_and_every_language(run_async):
    tg = MagicMock()
    tg.set_my_description = AsyncMock()
    tg.set_my_short_description = AsyncMock()
    run_async(bd.run_at_start(tg))
    for setter, field in (("set_my_description", "description"),
                          ("set_my_short_description", "short_description")):
        calls = _calls(tg, setter)
        assert all(c[field] == "" for c in calls)
        assert {c.get("language_code") for c in calls} == {None} | TG_CODES


def test_nothing_is_ever_set_to_a_text(run_async):
    tg = MagicMock()
    tg.set_my_description = AsyncMock()
    tg.set_my_short_description = AsyncMock()
    run_async(bd.run_at_start(tg))
    texts = [v for s in ("set_my_description", "set_my_short_description")
             for c in _calls(tg, s) for k, v in c.items() if k in ("description", "short_description")]
    assert texts and not any(texts)


def test_an_error_on_one_call_goes_on_and_never_raises(run_async):
    tg = MagicMock()
    tg.set_my_description = AsyncMock(side_effect=RuntimeError("boom"))
    tg.set_my_short_description = AsyncMock()
    run_async(bd.run_at_start(tg))
    assert tg.set_my_short_description.await_count == 1 + len(TG_CODES)


def test_start_returns_while_the_sync_is_still_pending(mk_bot, run_async, monkeypatch):
    monkeypatch.setattr("aipager.bot.lifecycle.CHAT_ID", "555")
    entered = []

    async def _blocked(_tg):
        entered.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(bd, "run_at_start", _blocked)
    tg = mk_bot()
    stub = MagicMock()
    stub.initialize = AsyncMock()
    stub.start = AsyncMock()
    stub.updater.start_polling = AsyncMock()
    stub.updater.stop = AsyncMock()
    stub.stop = AsyncMock()
    stub.shutdown = AsyncMock()
    tg._make_builder = MagicMock(return_value=MagicMock(build=MagicMock(return_value=stub)))
    tg._update_bot_commands = AsyncMock()

    async def _go():
        await asyncio.wait_for(tg.start(), timeout=5)
        await asyncio.sleep(0)
        task = tg._descriptions_task
        pending = not task.done()
        await tg.stop()
        await asyncio.sleep(0)
        return pending, task

    pending, task = run_async(_go())
    assert pending and entered == [1] and task.cancelled()


def _real_bot(mk_bot, monkeypatch):
    """The daemon's own bot (``_make_builder``) with a fake HTTP transport.
    Returns the bot and the list of (method, form fields) it sent."""
    from aipager.bot import lifecycle
    monkeypatch.setattr(lifecycle, "BOT_TOKEN", "123456:TEST")
    app = mk_bot()._make_builder().build()
    sent = []

    def handler(req):
        method = req.url.path.rsplit("/", 1)[-1]
        sent.append((method, dict(urllib.parse.parse_qsl(
            req.content.decode(), keep_blank_values=True))))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {
                "id": 1, "is_bot": True, "first_name": "b", "username": "b"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    for request in app.bot._request:
        request._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app.bot, sent


def test_the_real_bot_sends_an_empty_description_not_brackets(mk_bot, run_async, monkeypatch):
    bot, sent = _real_bot(mk_bot, monkeypatch)

    async def _go():
        await bot.initialize()
        await bd.clear_descriptions(bot)

    run_async(_go())
    fields = [(m, f) for m, f in sent if m in ("setMyDescription", "setMyShortDescription")]
    assert len(fields) == 2 * (1 + len(TG_CODES))
    for method, form in fields:
        key = "description" if method == "setMyDescription" else "short_description"
        assert form[key] == ""
    assert {f.get("language_code") for _, f in fields} == {None} | TG_CODES


def test_any_empty_text_parameter_stays_empty(mk_bot, run_async, monkeypatch):
    bot, sent = _real_bot(mk_bot, monkeypatch)

    async def _go():
        await bot.initialize()
        await bot.set_my_name(name="")
        await bot.answer_callback_query("1", text="")

    run_async(_go())
    forms = dict(sent)
    assert forms["setMyName"]["name"] == ""
    assert forms["answerCallbackQuery"]["text"] == ""

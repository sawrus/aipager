"""The Telegram side of the idle lock (:mod:`aipager.idle_lock`).

:func:`gate` runs first for every update (group -4). Unlocked, it notes
the activity of a served chat's member and lets the update go on. Locked,
it stops every update; in a member's private chat it answers ``/start``
with the code prompt and checks a 6-digit code. A code is never accepted
in a group: everyone there would see it.

:func:`run` is the background tick: when the quiet spell is over it
deletes the messages the lock knows of and sends the lock notice to each
private chat.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop

from aipager import i18n, idle_lock
from aipager.bot.transport import send_text
from aipager.i18n import Command, Msg

log = logging.getLogger(__name__)

TICK_SECONDS = 30
_DELETE_CHUNK = 100

LOCKED = Msg(
    "lock.locked",
    "🔒 aipager is locked after a quiet spell. Send {command} to unlock it.",
    note="sent to the owner's private chat when the idle lock closes; "
         "{command} is /start",
    eg={"command": "/start"})
ASK_CODE = Msg(
    "lock.ask_code",
    "Send the 6-digit code from your authenticator app.",
    note="the reply to /start while aipager is locked")
STILL_LOCKED = Msg(
    "lock.still_locked",
    "🔒 aipager is locked. Send {command} to unlock it.",
    note="the reply to anything else sent while locked; {command} is /start",
    eg={"command": "/start"})
UNLOCKED = Msg(
    "lock.unlocked",
    "🔓 Unlocked. Use {command} to see your sessions.",
    note="sent after a right code; {command} is /status",
    eg={"command": "/status"})
WRONG_CODE = Msg(
    "lock.wrong_code",
    "❌ Wrong code.",
    note="the reply to a wrong unlock code")
REPLAYED_CODE = Msg(
    "lock.replayed_code",
    "❌ This code was already used. Wait for the next one.",
    note="the reply to an unlock code that was already accepted once")
THROTTLED = Msg(
    "lock.throttled",
    "Too many wrong codes. Try again in 10 minutes.",
    note="the reply after five wrong unlock codes in ten minutes")
CALLBACK_LOCKED = Msg(
    "lock.callback_locked",
    "aipager is locked",
    note="the toast on a button tap while aipager is locked")


def _start() -> Command:
    return Command("/start")


def _is_member(bot, user_id, chat_id) -> bool:
    if user_id is None or chat_id is None:
        return False
    if getattr(bot, "scopes", None):
        return bot.member_in_chat(user_id, chat_id) is not None
    from aipager.config import CHAT_ID
    try:
        return int(chat_id) == int(CHAT_ID)
    except (TypeError, ValueError):
        return False


def private_chats(bot) -> list[int]:
    """The private chats the lock notice goes to."""
    if getattr(bot, "scopes", None):
        return [s.chat_id for s in bot.scopes if s.kind == "dm"]
    from aipager.config import CHAT_ID
    try:
        chat = int(CHAT_ID)
    except (TypeError, ValueError):
        return []
    return [chat] if chat > 0 else []


async def _say(bot, chat_id, msg, **params):
    with idle_lock.service():
        return await send_text(bot._app.bot, chat_id, i18n.for_chat(chat_id).text(msg, **params))


async def _delete(bot, chat_id, message_id) -> None:
    app = bot._app
    try:
        await app.bot.delete_message(chat_id, message_id)
    except Exception:
        log.debug("idle lock: could not delete message %s", message_id, exc_info=True)


async def gate(bot, update: Update, context) -> None:
    """Group -4: note activity, or, while locked, stop the update."""
    lock = idle_lock.LOCK
    lock.reload_if_changed()
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else None
    user_id = user.id if user else None
    member = _is_member(bot, user_id, chat_id)
    msg = update.message
    if member and msg is not None:
        lock.note_message(chat_id, msg.message_id)

    if not lock.is_locked():
        if member and lock.enabled:
            lock.touch()
        return

    # A group Telegram upgraded to a supergroup must still be followed.
    if msg is not None and (msg.migrate_to_chat_id or msg.migrate_from_chat_id):
        return

    query = update.callback_query
    if query is not None:
        try:
            await query.answer(i18n.for_chat(chat_id).text(CALLBACK_LOCKED))
        except Exception:
            log.debug("idle lock: could not answer a tap", exc_info=True)
        raise ApplicationHandlerStop

    private = chat is not None and chat.type == "private"
    if not (member and private and msg is not None and msg.text):
        raise ApplicationHandlerStop

    text = msg.text.strip()
    if text.split("@")[0] == "/start":
        await _say(bot, chat_id, ASK_CODE)
        raise ApplicationHandlerStop

    verdict = lock.try_code(text)
    if verdict == "not_a_code":
        await _say(bot, chat_id, STILL_LOCKED, command=_start())
    elif verdict == "ok":
        await _delete(bot, chat_id, msg.message_id)  # never leave a code behind
        await _say(bot, chat_id, UNLOCKED, command=Command("/status"))
    elif verdict == "throttled":
        await _say(bot, chat_id, THROTTLED)
    else:
        await _delete(bot, chat_id, msg.message_id)
        await _say(bot, chat_id, REPLAYED_CODE if verdict == "replayed" else WRONG_CODE)
    raise ApplicationHandlerStop


async def lock_now(bot) -> None:
    """Lock, delete the known messages, send the notice."""
    wipe = idle_lock.LOCK.lock()
    app = bot._app
    for chat_id, ids in wipe.items():
        for i in range(0, len(ids), _DELETE_CHUNK):
            chunk = ids[i:i + _DELETE_CHUNK]
            try:
                await app.bot.delete_messages(chat_id, chunk)
            except Exception:
                log.info("idle lock: bulk delete failed in %s; one by one", chat_id,
                         exc_info=True)
                for message_id in chunk:
                    await _delete(bot, chat_id, message_id)
    for chat_id in private_chats(bot):
        await _say(bot, chat_id, LOCKED, command=_start())


async def tick(bot) -> None:
    lock = idle_lock.LOCK
    lock.forget_cache()
    lock.reload_if_changed()
    if lock.due():
        await lock_now(bot)
    lock.save_if_dirty()


async def run(bot) -> None:
    """The background tick, until cancelled."""
    while True:
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("idle lock: tick failed", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)

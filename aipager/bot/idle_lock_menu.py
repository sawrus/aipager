"""/settings -> 🔒 Idle lock: how long a quiet spell is before aipager asks
for an authenticator code (:mod:`aipager.idle_lock`).

Three choices: 1 h, 8 h, 16 h (:data:`CHOICES_MIN`). The time is the one
``aipager.yaml`` keeps (``security.idle_lock.after_min``), so ``aipager
lock enable --after-min`` and this page write the same value. A value
outside the list (an older install) stays in effect and is shown as
"custom: N min" with no ✅ until one of the three is picked. Install-wide,
not per chat: only an owner or admin may change it, in a DM too.

Callback-data contract (``_:`` sentinel, ``set:`` is VIEW in tap_gate):
    _:set:il            -> the page
    _:set:il:<minutes>  -> set it (60, 480 or 960; admin only)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from aipager import i18n, idle_lock
from aipager.bot import appearance, language_menu, settings_menu
from aipager.bot.transport import calling_chat_id, edit_text
from aipager.i18n import Command, Msg, Safe

if TYPE_CHECKING:
    from telegram import CallbackQuery, Update

    from aipager.bot.core import TelegramBot

log = logging.getLogger(__name__)

#: The times a person may pick, in minutes. ``aipager lock enable
#: --after-min`` accepts the same three.
CHOICES_MIN = idle_lock.AFTER_MIN_CHOICES

TITLE = Msg("set.il.title", "🔒 Idle lock",
            note="/settings section: the quiet time after which aipager asks for a code")
BUTTON = Msg("set.btn.idle_lock", "{title}: {value}",
             note="/settings button: the idle lock's title and its current time",
             eg={"title": "🔒 Idle lock", "value": "1 h"})
HOURS = Msg("set.opt.il.hours", "{n} h", note="A time in hours, as on a button", eg={"n": 8})
CUSTOM = Msg("set.opt.il.custom", "custom: {n} min",
             note="The idle lock time is none of the three choices; {n} is minutes",
             eg={"n": 45})
OFF = Msg("set.opt.il.off", "{value}, lock off",
          note="The idle lock time with a note that the lock is turned off",
          eg={"value": "1 h"})
PAGE_ON = Msg("set.text.il.on",
              "🔒 <b>Idle lock</b>\n\nHow long aipager stays quiet before it asks for a "
              "code from your authenticator app.",
              note="Top of /settings -> Idle lock, the lock is on")
PAGE_OFF = Msg("set.text.il.off",
               "🔒 <b>Idle lock</b>\n\nThe lock is off. The time you pick is kept for "
               "when it is turned on with <code>aipager lock enable</code>.",
               note="Top of /settings -> Idle lock, the lock is off; keep the command as it is")
ADMIN_ONLY = Msg("toast.il_admin_only", "Only an admin can change the idle lock.",
                 note="Pop-up when a member without rights taps an idle lock time")
NOT_SAVED = Msg("toast.il_not_saved", "Could not save the idle lock time.",
                note="Pop-up: the setting file could not be written")


def _settings() -> dict:
    from aipager.scope import load_idle_lock
    return load_idle_lock()


def time_text(tr, minutes: int) -> str:
    """``1 h`` for a choice, ``custom: N min`` for any other value."""
    if minutes in CHOICES_MIN:
        return tr.text(HOURS, n=minutes // 60)
    return tr.text(CUSTOM, n=minutes)


def button_text(tr) -> str:
    """The /settings root row: title and current time (and that it is off)."""
    settings = _settings()
    value = time_text(tr, settings["after_min"])
    if not settings["enabled"]:
        value = tr.text(OFF, value=Safe(value))
    return tr.text(BUTTON, title=Safe(tr.text(TITLE)), value=Safe(value))


def render_page(chat_id: int, *, language: str | None = None):
    """The three times with the current one marked, plus Back."""
    tr = i18n.get(language) if language else i18n.for_chat(chat_id)
    settings = _settings()
    current = settings["after_min"]
    marked = appearance.choice_style(chat_id)
    rows = []
    for minutes in CHOICES_MIN:
        here = minutes == current
        rows.append([InlineKeyboardButton(
            tr.text(HOURS, n=minutes // 60) + (" ✅" if here else ""),
            callback_data=f"_:set:il:{minutes}",
            style=marked if here else None)])
    rows.append([InlineKeyboardButton(tr.text(language_menu.BACK),
                                      callback_data="_:set:back")])
    text = tr.html(PAGE_ON if settings["enabled"] else PAGE_OFF)
    if current not in CHOICES_MIN:
        text += "\n\n" + tr.html(CUSTOM, n=current)
    return text, InlineKeyboardMarkup(rows)


async def handle_callback(bot: TelegramBot, update: Update, query: CallbackQuery,
                          session_name: str, action: str) -> bool:
    """True iff this callback is ``_:set:il...``. Viewing is open; a change
    needs an owner or admin, in a DM too (the setting is install-wide)."""
    if session_name != "_" or not (action == "set:il" or action.startswith("set:il:")):
        return False
    parts = action.split(":")[2:]
    chat_id = calling_chat_id(update)
    scope = chat_id or 0
    shared = i18n.for_update(update)
    person = i18n.for_person(update)

    async def _show(by: str = "") -> None:
        text, kb = render_page(scope, language=shared.code)
        if by:
            text += "\n\n" + shared.html(settings_menu.CHANGED_BY, person=Command(by))
        try:
            await edit_text(query, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.debug("idle lock: page edit failed", exc_info=True)

    if not parts:
        await _show()
        return True
    if len(parts) != 1 or parts[0] not in {str(m) for m in CHOICES_MIN}:
        await bot._safe_answer(query, person.text(language_menu.INVALID_VALUE))
        return True
    if not bot._is_admin(update):
        await bot._safe_answer(query, person.text(ADMIN_ONLY), show_alert=True)
        return True
    from aipager.scope import ScopeConfigError, dump_idle_lock
    settings = _settings()
    settings["after_min"] = int(parts[0])
    try:
        dump_idle_lock(settings)
    except (ScopeConfigError, OSError):
        log.warning("idle lock: could not save the time", exc_info=True)
        await bot._safe_answer(query, person.text(NOT_SAVED), show_alert=True)
        return True
    idle_lock.LOCK.forget_cache()  # takes effect now, not after the 10 s cache
    tapper = getattr(query, "from_user", None)
    by = bot._actor_label(getattr(tapper, "id", None), chat_id, tapper)
    await _show(by=by or "")
    return True


__all__ = ["BUTTON", "CHOICES_MIN", "TITLE", "button_text", "handle_callback",
           "render_page", "time_text"]

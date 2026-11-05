"""The bot's description is cleared, never set.

Telegram shows a bot's description on its empty chat screen before the
first message, and its short description on its profile. aipager used to
set both per language; that text is now unwanted. At start, in the
background and at the lowest priority, aipager sets the description and the
short description to an empty text for the default (no ``language_code``)
and for each language of the project. Telegram treats an empty text as
"none". Nothing sets a description any more; there is no switch to bring it
back. The texts (``DESCRIPTION``, ``SHORT``) stay only as translated
strings. Never blocks start, never raises.
"""

from __future__ import annotations

import asyncio
import logging

from aipager.i18n import Msg
from aipager.i18n.locales import LOCALES, enabled_codes

log = logging.getLogger(__name__)

__all__ = ["DESCRIPTION", "SHORT", "clear_descriptions", "run_at_start"]

DESCRIPTION = Msg(
    "botdesc.description",
    "aipager connects this bot to Claude Code sessions on its owner's computer. From here "
    "you can follow what Claude is doing, answer its questions and permission prompts, and "
    "send it new prompts. Only people the owner has added can use it.",
    note="The bot's description in Telegram (shown on the empty chat screen before the "
         "first message). Plain text, at most 512 characters.")
SHORT = Msg(
    "botdesc.short", "Control Claude Code sessions from Telegram.",
    note="The bot's short description in Telegram (its profile and share card). Plain text, "
         "at most 120 characters.")

_KINDS = (
    # (setter, field)
    ("set_my_description", "description"),
    ("set_my_short_description", "short_description"),
)


def _ornament() -> dict | None:
    from aipager.bot.flood_budget import PRIORITY_ORNAMENT, rate_limit_args
    return rate_limit_args(priority=PRIORITY_ORNAMENT)


async def clear_descriptions(tg_bot) -> int:
    """Set an empty description and short description for the default and
    for each language. Returns how many calls succeeded."""
    langs: list[str | None] = [None]
    for code in enabled_codes():
        loc = LOCALES.get(code)
        tg = getattr(loc, "telegram_code", None)
        if tg and tg not in langs:
            langs.append(tg)
    done = 0
    for setter, field in _KINDS:
        for tg in langs:
            kwargs = {field: ""}
            if tg:
                kwargs["language_code"] = tg
            try:
                await getattr(tg_bot, setter)(**kwargs, rate_limit_args=_ornament())
                done += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one language never stops the rest
                log.info("bot %s for %s not cleared (%s)", field, tg, type(e).__name__)
    return done


async def run_at_start(tg_bot) -> None:
    """The start-time task: clear the descriptions. Never raises."""
    try:
        await clear_descriptions(tg_bot)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - background: log and carry on
        log.info("bot descriptions not cleared", exc_info=True)

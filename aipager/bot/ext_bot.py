"""The daemon's ExtBot: an empty text parameter stays an empty text.

PTB 22.8, with ``Defaults`` set (the daemon sets them for link previews),
runs ``ExtBot._insert_defaults`` on every call. Its step for poll options
checks ``isinstance(val, Sequence) and all(isinstance(o, InputPollOption)
for o in val)``. A ``str`` is a ``Sequence``, and ``all()`` of an empty one
is true, so every ``""`` parameter became ``[]`` and went to Telegram as
the text ``"[]"``. ``setMyDescription(description="")`` set the bot's
description to ``[]`` instead of clearing it. A non-empty text is never
touched there, so only ``""`` needs to be put back.
"""

from __future__ import annotations

from telegram.ext import ApplicationBuilder, ExtBot

__all__ = ["DaemonApplicationBuilder", "EmptyTextExtBot"]


class EmptyTextExtBot(ExtBot):
    """An ``ExtBot`` that sends an empty text parameter as ``""``."""

    __slots__ = ()

    def _insert_defaults(self, data: dict[str, object]) -> None:
        empty = [k for k, v in data.items() if isinstance(v, str) and not v]
        super()._insert_defaults(data)
        for key in empty:
            data[key] = ""


class DaemonApplicationBuilder(ApplicationBuilder):
    """``ApplicationBuilder`` whose bot is an :class:`EmptyTextExtBot`.

    PTB builds the bot itself, with every setting of the builder; only its
    class changes here, so no setting can be lost."""

    __slots__ = ()

    def _build_ext_bot(self) -> ExtBot:
        bot = super()._build_ext_bot()
        bot.__class__ = EmptyTextExtBot
        return bot

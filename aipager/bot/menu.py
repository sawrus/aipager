"""The main keyboard's menu rows and the chat clean-up a menu tap does (fork
``sawrus/aipager``).

Every / command a person uses has a button on the main keyboard of a
private chat: the words aipager always had (status, stop, new) and the
rows here (Resume, Now, Mode, Settings, Language, Help). A tap runs the
command, as typing it would.

A tap on a menu button that opens a view (status, new and every button
here except Now) first deletes the chat's messages from the previous
clean-up up to the tap, so the chat shows only what the tap brings. Kept:
the tapped button's own message (an empty private chat makes Telegram
show its Start button), the live busy cards and the messages of open
prompts, which a person still answers. Stop and Now act on the work in
progress, so their taps keep the chat as it is. ``menu.clean_chat: false``
in ``aipager.yaml`` turns the clean-up off.

The first clean-up looks back :data:`CLEAN_SPAN` message ids: a private
chat numbers both sides' messages in one sequence. Telegram deletes only
messages younger than 48 hours and skips ids it cannot find.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aipager._test_guard import check_write
from aipager.i18n import Msg

log = logging.getLogger(__name__)

#: Message ids looked back the first time a chat is cleaned.
CLEAN_SPAN = 2000
#: Ids per deleteMessages call (the Bot API's limit).
DELETE_CHUNK = 100
#: Where each chat's last cleaned message id is kept.
MENU_STATE_PATH = Path.home() / ".local" / "state" / "aipager" / "menu.json"

#: action -> button, in the keyboard's order (three per row).
MENU = (
    ("resume", Msg("kb.menu.resume", "📚 Resume",
                   note="Main keyboard button: runs /resume (continue an ended session)")),
    ("now", Msg("kb.menu.now", "⏩ Now",
                note="Main keyboard button: runs /now (send queued messages to Claude now)")),
    ("mode", Msg("kb.menu.mode", "🔐 Mode",
                 note="Main keyboard button: runs /mode (Ask, Auto or Full access)")),
    ("settings", Msg("kb.menu.settings", "⚙️ Settings",
                     note="Main keyboard button: runs /settings")),
    ("language", Msg("kb.menu.language", "🌐 Language",
                     note="Main keyboard button: runs /language")),
    ("help", Msg("kb.menu.help", "❓ Help",
                 note="Main keyboard button: runs /help")),
)
#: The bot's handler for each action.
HANDLERS = {"resume": "_handle_resume_cmd", "now": "_handle_now_cmd",
            "mode": "_handle_mode_cmd", "settings": "_handle_settings_cmd",
            "language": "_handle_language_cmd", "help": "_handle_help_cmd"}
#: Taps that clean the chat first: the ones that open a view.
CLEANING = frozenset({"status", "new", "resume", "mode", "settings", "language", "help"})


def action(text: str) -> str | None:
    """The menu action of a button's text, in any enabled language
    (the whole text, normalized as every keyboard text is)."""
    from aipager.bot import kbwords
    t = kbwords.norm(text)
    for name, msg in MENU:
        if any(t == kbwords.norm(label) for label in kbwords._labels(msg)):
            return name
    return None


def rows(tr, button) -> list[list]:
    """The menu rows in *tr*'s language, each label made a button by
    *button*."""
    labels = [tr.text(msg) for _name, msg in MENU]
    return [[button(label) for label in labels[i:i + 3]] for i in range(0, len(labels), 3)]


def enabled() -> bool:
    """Whether a menu tap cleans the chat: on unless ``menu.clean_chat``
    in ``aipager.yaml`` is a YAML ``false``. Never raises."""
    from aipager import scope
    try:
        block = scope._raw_yaml(scope.CONFIG_PATH).get("menu")
    except Exception:
        return True
    return not (isinstance(block, dict) and block.get("clean_chat") is False)


def _load() -> dict:
    try:
        data = json.loads(MENU_STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    from aipager.private_file import write_private
    try:
        check_write(MENU_STATE_PATH)
        write_private(MENU_STATE_PATH, json.dumps(data), dir_mode=0o700)
    except OSError:
        log.warning("menu: could not save %s", MENU_STATE_PATH, exc_info=True)


def to_clean(chat_id: int, message_id: int, keep=frozenset()) -> list[int]:
    """The message ids to delete before the tap *message_id*: from the last
    clean-up of *chat_id* (else :data:`CLEAN_SPAN` back) up to the one
    before the tap, without *keep*. Remembers the tap as the new start."""
    data = _load()
    last = data.get(str(chat_id))
    start = last + 1 if isinstance(last, int) and not isinstance(last, bool) else 0
    start = max(1, start, message_id - CLEAN_SPAN)
    data[str(chat_id)] = message_id
    _save(data)
    return [i for i in range(start, message_id) if i not in keep]


def kept_messages(bot, chat_id: int) -> set[int]:
    """Messages a clean-up leaves: the live busy cards and the messages of
    open prompts of *chat_id*'s sessions."""
    keep: set[int] = set()
    try:
        sessions = bot.registry.all_sessions(chat_id).values()
    except Exception:
        return keep
    for sess in sessions:
        records = [getattr(sess, "pending_prompt_msg", None)]
        records += list(getattr(sess, "extra_prompts", None) or [])
        ids = [getattr(sess, "busy_msg_id", None)]
        ids += [r.get("msg_id") for r in records if isinstance(r, dict)]
        keep |= {i for i in ids if isinstance(i, int) and not isinstance(i, bool)}
    return keep


async def clean(bot, update, tapped: str) -> int:
    """Delete the chat's messages before a menu tap *tapped* (an action
    name) in a private chat; the number of ids asked to be deleted. Never
    raises: a failed delete only leaves messages in place."""
    if tapped not in CLEANING or not enabled():
        return 0
    msg = getattr(update, "message", None)
    chat = getattr(msg, "chat", None)
    if msg is None or getattr(chat, "type", "") != "private":
        return 0
    chat_id = msg.chat_id
    ids = to_clean(chat_id, msg.message_id, kept_messages(bot, chat_id))
    app = bot._app
    for i in range(0, len(ids), DELETE_CHUNK):
        try:
            await app.bot.delete_messages(chat_id, ids[i:i + DELETE_CHUNK])
        except Exception:
            log.info("menu: delete failed in %s", chat_id, exc_info=True)
    return len(ids)


async def run(bot, update, ctx, tapped: str) -> None:
    """A menu tap *tapped*: clean the chat, then run its command as if the
    bare command had been typed (the handlers read the message's text, so
    it becomes ``/<command>``: the button's label is no argument)."""
    await clean(bot, update, tapped)
    msg = update.message
    with msg._unfrozen():
        msg.text = f"/{tapped}"
    await getattr(bot, HANDLERS[tapped])(update, ctx)

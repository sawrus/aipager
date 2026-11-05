"""What a log line may say about a person's text: its kind and length.

A prompt, a voice transcript or a queued message is the user's own
content, and the journal is no place for it (``journalctl`` keeps it for
weeks, other admins of the machine read it, problem reports quote it).
Log :func:`shape` of the text instead of the text. Imports nothing, so
the hook's import closure (``aipager.state``) may use it.
"""

from __future__ import annotations

import re

_COMMAND = re.compile(r"/([a-z_]{1,32})(?:@\w+)?")


def shape(text) -> str:
    """``"text, 42 chars"``, ``"/model, 12 chars"`` (a bare command name
    only, never its arguments), or the type's name for a non-string."""
    if not isinstance(text, str):
        return type(text).__name__
    words = text.split(None, 1)
    match = _COMMAND.fullmatch(words[0]) if words else None
    kind = f"/{match.group(1)}" if match else "text"
    return "%s, %d %s" % (kind, len(text), "chars")

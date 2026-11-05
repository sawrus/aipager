"""No log line carries a person's text (aipager.text_shape).

A prompt, a voice transcript or a queued message used to reach the
journal as its first 50-80 characters. Every log call now names only the
text's kind and length. The scan below fails when a log call takes a
variable that holds such text again."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path


from aipager.text_shape import shape

PACKAGE = Path(__file__).resolve().parent.parent / "aipager"
#: Names that hold what a person (or Claude) wrote.
TEXT_NAMES = frozenset({"text", "prompt", "prompt_text", "queued_text", "transcript",
                        "caption", "user_text", "reply_context"})
LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def test_shape_says_kind_and_length_only():
    assert shape("delete the prod database") == "text, 24 chars"
    assert shape("/model opus secret-arg") == "/model, 22 chars"
    assert shape("/Weird-Name x") == "text, 13 chars"
    assert shape("") == "text, 0 chars"
    assert shape(None) == "NoneType"


def _text_source(node) -> str | None:
    """The text variable *node* reads (``text``, ``text[:80]``,
    ``msg.text``, ``prompt.strip()``), else None."""
    while True:
        if isinstance(node, ast.Name):
            return node.id if node.id in TEXT_NAMES else None
        if isinstance(node, ast.Attribute):
            if node.attr in TEXT_NAMES:
                return node.attr
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            node = node.func.value
        else:
            return None


def _offenders():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "i18n" in path.relative_to(PACKAGE).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LEVELS and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"log", "logger", "_log", "LOG"}):
                continue
            for arg in node.args[1:]:
                name = _text_source(arg)
                if name:
                    found.append(f"{path.relative_to(PACKAGE)}:{node.lineno}: {name}")
    return found


def test_no_log_call_takes_a_persons_text():
    assert _offenders() == []


def test_the_scan_sees_a_sliced_prompt(tmp_path, monkeypatch):
    """The scan itself: a log call like the old ones is found."""
    (tmp_path / "mod.py").write_text(
        'log.info("[%s] Sent text: %s", label, text[:80])\n', encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.PACKAGE", tmp_path)
    assert _offenders() == ["mod.py:1: text"]


def test_send_text_and_enter_logs_no_text(monkeypatch, caplog):
    """inject.send_text_and_enter: the one line every prompt passed."""
    from aipager.dtach import inject

    async def ok(*args, **kwargs):
        return True, ""

    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr(inject, "_run", ok)
    monkeypatch.setattr(inject, "_key_gap_sleep", no_wait)
    with caplog.at_level(logging.INFO, logger="aipager.dtach.inject"):
        sent = asyncio.new_event_loop().run_until_complete(
            inject.send_text_and_enter("x1", "my bank password is hunter2"))
    assert sent
    assert "Sent text (text, 27 chars)" in caplog.text
    assert "hunter2" not in caplog.text

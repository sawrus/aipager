"""The idle lock (aipager.idle_lock, aipager.bot.idle_gate): TOTP codes,
the quiet-spell clock, the outbound gate in the rate limiter, the inbound
gate for Telegram updates, the Mini App's 423 and the config block that
``aipager config`` must keep."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
import yaml
from telegram.ext import ApplicationHandlerStop

from aipager import idle_lock, scope
from aipager.bot import idle_gate
from aipager.bot.flood import FloodMuted, IdleLocked
from aipager.bot.flood_budget import BudgetRateLimiter

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"
OWNER = 178
CHAT = 178


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _config(enabled=True, after_min=60, **extra):
    data = {"schema_version": 3, "bot_token": "1:x", "scopes": [
        {"kind": "dm", "chat_id": CHAT, "label": "owner DM",
         "members": [{"id": OWNER, "label": "owner", "role": "owner"}]}]}
    data["security"] = {"idle_lock": {"enabled": enabled, "after_min": after_min}}
    data.update(extra)
    scope.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    scope.CONFIG_PATH.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def armed():
    """An enabled lock with the RFC secret, last active now."""
    _config()
    idle_lock.write_secret(RFC_SECRET)
    lock = idle_lock.LOCK
    lock.touch(time.time())
    lock.save()
    return lock


# ---- TOTP ---------------------------------------------------------------------

@pytest.mark.parametrize("t, want", [
    (59, "94287082"), (1111111109, "07081804"), (20000000000, "65353130")])
def test_totp_matches_rfc6238(t, want):
    assert idle_lock.totp_code(RFC_SECRET, t // 30, 8) == want


def test_verify_accepts_one_step_each_way_and_refuses_replay():
    now = 1_000_000 * 30 + 5
    cur = int(now // 30)
    code = idle_lock.totp_code
    assert idle_lock.verify_code(RFC_SECRET, code(RFC_SECRET, cur), now, 0) == (cur, "ok")
    assert idle_lock.verify_code(RFC_SECRET, code(RFC_SECRET, cur - 1), now, 0)[1] == "ok"
    assert idle_lock.verify_code(RFC_SECRET, code(RFC_SECRET, cur + 1), now, 0)[1] == "ok"
    assert idle_lock.verify_code(RFC_SECRET, code(RFC_SECRET, cur - 2), now, 0)[1] == "denied"
    assert idle_lock.verify_code(RFC_SECRET, code(RFC_SECRET, cur), now, cur) == (None, "replayed")


def test_parse_code():
    assert idle_lock.parse_code(" 123 456 ") == "123456"
    assert idle_lock.parse_code("12345") is None
    assert idle_lock.parse_code("/start") is None


def test_the_secret_folder_is_narrowed_to_0700():
    """``~/.config/aipager`` made 0775 by an older install: mkdir's mode
    applies only when it creates the folder."""
    idle_lock.TOTP_PATH.parent.mkdir(parents=True, exist_ok=True)
    idle_lock.TOTP_PATH.parent.chmod(0o775)
    idle_lock.write_secret(idle_lock.new_secret())
    assert idle_lock.TOTP_PATH.parent.stat().st_mode & 0o777 == 0o700


def test_writing_aipager_yaml_narrows_its_folder():
    _config()
    scope.CONFIG_PATH.parent.chmod(0o775)
    scope.dump_idle_lock({"enabled": True, "after_min": 30})
    assert scope.CONFIG_PATH.parent.stat().st_mode & 0o777 == 0o700


def test_secret_round_trip_and_uri():
    secret = idle_lock.new_secret()
    idle_lock.write_secret(secret)
    assert idle_lock.read_secret() == secret
    assert idle_lock.TOTP_PATH.stat().st_mode & 0o777 == 0o600
    assert f"secret={secret}" in idle_lock.otpauth_uri(secret)


# ---- config -------------------------------------------------------------------

def test_config_defaults_off_and_takes_only_a_real_boolean():
    assert scope.load_idle_lock() == {"enabled": False, "after_min": 60}
    _config(enabled="yes please", after_min=0)
    assert scope.load_idle_lock() == {"enabled": False, "after_min": 60}
    _config(enabled=True, after_min=15)
    assert scope.load_idle_lock() == {"enabled": True, "after_min": 15}


def test_dump_idle_lock_keeps_the_rest():
    _config(enabled=False, miniapp={"enabled": True, "port": 8766})
    scope.dump_idle_lock({"enabled": True, "after_min": 30})
    raw = yaml.safe_load(scope.CONFIG_PATH.read_text())
    assert raw["security"]["idle_lock"] == {"enabled": True, "after_min": 30}
    assert raw["miniapp"]["port"] == 8766
    assert raw["scopes"][0]["chat_id"] == CHAT


def test_dump_scopes_keeps_the_idle_lock():
    """`aipager config` rewrites the file: the lock must survive it."""
    _config(enabled=True, after_min=45)
    scopes, token = scope.load_scopes(scope.CONFIG_PATH)
    scope.dump_scopes(scopes, token, scope.CONFIG_PATH)
    assert scope.load_idle_lock() == {"enabled": True, "after_min": 45}


# ---- state --------------------------------------------------------------------

def test_off_without_a_secret_even_when_enabled():
    _config()
    assert not idle_lock.LOCK.enabled
    idle_lock.LOCK.lock()
    assert not idle_lock.LOCK.is_locked()


def test_due_after_the_quiet_spell(armed):
    now = time.time()
    armed.touch(now)
    assert not armed.due(now + 60 * 60)
    assert armed.due(now + 60 * 60 + 1)
    armed.lock(now + 60 * 60 + 1)
    assert not armed.due(now + 10 ** 6)  # already locked


def test_wipe_plan_takes_young_messages_and_wiped_forgets_them(armed):
    now = time.time()
    armed.note_message(CHAT, 1, now - 49 * 3600)
    armed.note_message(CHAT, 2, now - 3600)
    armed.note_message(-100, 3, now)
    armed.lock(now)
    assert armed.is_locked() and armed.wipe_pending
    plan = armed.wipe_plan(now=now)
    assert plan == {CHAT: [2], -100: [3]}
    armed.wiped(plan, now=now)
    assert not armed.wipe_pending
    assert armed.wipe_plan(now=now) == {}


def test_lock_keeps_the_messages_until_they_are_wiped(armed):
    """A wipe a stop cuts short must not lose what it was to delete."""
    armed.note_message(-100, 3)
    armed.lock()
    fresh = idle_lock.IdleLock()
    assert fresh.wipe_plan() == {-100: [3]}
    assert fresh.wipe_pending


def test_a_failed_delete_stays_for_the_next_wipe(armed):
    armed.note_message(-100, 3)
    armed.note_message(-100, 4)
    armed.lock()
    plan = armed.wipe_plan()
    armed.wiped(plan, {-100: {4}})
    assert armed.wipe_plan() == {-100: [4]}


def test_seeing_a_message_again_neither_adds_nor_rejuvenates_it(armed):
    """A streaming card edited a thousand times must not push real
    messages out of the tracker, nor stay "young" for ever."""
    now = time.time()
    armed.note_message(-100, 1, now - 49 * 3600)
    for _ in range(idle_lock.MAX_TRACKED + 1):
        armed.note_message(-100, 1, now)
    armed.note_message(-100, 2, now)
    assert len(armed._state["sent"]) == 2
    assert armed.wipe_plan(now=now) == {-100: [2]}


def test_a_private_chat_is_wiped_as_a_whole_range(armed):
    """A private chat numbers both sides' messages in one sequence: the
    wipe deletes every id since the previous one, tracked or not."""
    armed.note_message(CHAT, 10)
    armed.note_message(CHAT, 15)
    armed.note_message(-100, 30)
    plan = armed.wipe_plan([CHAT])
    assert plan == {CHAT: list(range(10, 16)), -100: [30]}
    armed.wiped(plan, private=[CHAT])
    armed.note_message(CHAT, 20)
    assert armed.wipe_plan([CHAT]) == {CHAT: list(range(16, 21))}


def test_a_private_range_is_capped(armed):
    armed._state["wiped_upto"] = {str(CHAT): 1}
    armed.note_message(CHAT, 10 ** 6)
    ids = armed.wipe_plan([CHAT])[CHAT]
    assert len(ids) == idle_lock.WIPE_SPAN and ids[-1] == 10 ** 6


def test_a_reload_keeps_messages_noted_since_the_last_save(armed):
    """`aipager lock now` writes the file while the daemon holds unsaved
    notes: the reload must not drop them from the next wipe."""
    armed.note_message(-100, 5)
    assert armed._dirty
    other = idle_lock.IdleLock()
    other.load()
    other.make_due()
    time.sleep(0.01)
    armed.reload_if_changed()
    assert armed.due()
    assert armed.wipe_plan() == {-100: [5]}


def test_state_survives_a_restart(armed):
    armed.lock()
    fresh = idle_lock.IdleLock()
    assert fresh.is_locked()
    assert idle_lock.LOCK_STATE_PATH.stat().st_mode & 0o777 == 0o600


def test_code_unlocks_once_and_wrong_codes_throttle(armed):
    now = 1_000_000 * 30 + 5
    armed.lock(now)
    good = idle_lock.totp_code(RFC_SECRET, int(now // 30))
    assert armed.try_code("hello", now) == "not_a_code"
    assert armed.try_code(good, now) == "ok"
    assert not armed.is_locked()
    armed.lock(now)
    assert armed.try_code(good, now) == "replayed"
    for _ in range(idle_lock.MAX_FAILS - 1):
        assert armed.try_code("000000", now) == "denied"
    newer = idle_lock.totp_code(RFC_SECRET, int(now // 30) + 1)
    assert armed.try_code(newer, now) == "throttled"
    assert armed.is_locked()
    later = now + idle_lock.FAIL_WINDOW + 1
    assert armed.try_code(idle_lock.totp_code(RFC_SECRET, int(later // 30)), later) == "ok"


def test_reload_picks_up_the_cli(armed):
    other = idle_lock.IdleLock()
    other.load()
    other.make_due()
    time.sleep(0.01)
    armed.reload_if_changed()
    assert armed.due()


# ---- the outbound gate (rate limiter) ---------------------------------------

def _limited_call(endpoint, data, result=None):
    limiter = BudgetRateLimiter()
    callback = AsyncMock(return_value=result if result is not None else True)
    out = _run(limiter.process_request(callback, (), {}, endpoint, data, None))
    return out, callback


def test_limiter_refuses_a_chat_call_while_locked(armed):
    armed.lock()
    with pytest.raises(IdleLocked) as info:
        _limited_call("sendMessage", {"chat_id": CHAT, "text": "secret output"})
    assert isinstance(info.value, FloodMuted)


@pytest.mark.parametrize("endpoint, data", [
    ("deleteMessages", {"chat_id": CHAT, "message_ids": "[1]"}),
    ("answerCallbackQuery", {"callback_query_id": "1"}),
    ("getMe", {}),
])
def test_limiter_lets_the_harmless_calls_through(armed, endpoint, data):
    armed.lock()
    _, callback = _limited_call(endpoint, data)
    callback.assert_awaited_once()


def test_limiter_lets_the_lock_itself_speak(armed):
    armed.lock()
    with idle_lock.service():
        _, callback = _limited_call("sendMessage", {"chat_id": CHAT, "text": "🔒"})
    callback.assert_awaited_once()


def test_limiter_records_sent_messages_for_the_wipe(armed):
    _limited_call("sendMessage", {"chat_id": CHAT, "text": "hi"},
                  result={"message_id": 42, "chat": {"id": CHAT}})
    assert armed.wipe_plan() == {CHAT: [42]}


def test_limiter_does_not_note_an_edit_again(armed):
    old = time.time() - 49 * 3600
    armed.note_message(-100, 42, old)
    _limited_call("editMessageText", {"chat_id": -100, "message_id": 42, "text": "x"},
                  result={"message_id": 42, "chat": {"id": -100}})
    _limited_call("editMessageText", {"chat_id": -100, "message_id": 43, "text": "x"},
                  result={"message_id": 43, "chat": {"id": -100}})
    assert armed.wipe_plan() == {}


def test_limiter_records_a_copy_under_the_calls_chat(armed):
    """copyMessage returns only a MessageId, with no chat in it."""
    _limited_call("copyMessage", {"chat_id": -100, "from_chat_id": CHAT, "message_id": 1},
                  result={"message_id": 77})
    assert armed.wipe_plan() == {-100: [77]}


# ---- the inbound gate (Telegram updates) -------------------------------------

class _Bot:
    def __init__(self):
        self.scopes = scope.load_scopes(scope.CONFIG_PATH)[0]
        self._app = SimpleNamespace(bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)),
            delete_message=AsyncMock(), delete_messages=AsyncMock()))

    def member_in_chat(self, user_id, chat_id):
        for s in self.scopes:
            if s.chat_id == chat_id:
                return next((m for m in s.members if m.id == user_id), None)
        return None

    def said(self):
        return [c.args[1] for c in self._app.bot.send_message.await_args_list]


def _update(text=None, *, user=OWNER, chat=CHAT, chat_type="private", callback=False):
    msg = None if callback else SimpleNamespace(
        message_id=500, text=text, migrate_to_chat_id=None, migrate_from_chat_id=None)
    query = SimpleNamespace(answer=AsyncMock()) if callback else None
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, type=chat_type),
        effective_user=SimpleNamespace(id=user), message=msg, callback_query=query)


def _gate(bot, update):
    return _run(idle_gate.gate(bot, update, None))


def test_gate_notes_activity_when_unlocked(armed):
    bot = _Bot()
    armed._state["last_activity"] = 0.0
    _gate(bot, _update("hello"))
    assert not armed.due()


def test_gate_ignores_strangers_activity(armed):
    bot = _Bot()
    armed._state["last_activity"] = 0.0
    _gate(bot, _update("hello", user=999, chat=999))
    assert armed.due()


def test_gate_stops_everything_while_locked(armed):
    bot = _Bot()
    armed.lock()
    with pytest.raises(ApplicationHandlerStop):
        _gate(bot, _update("rm -rf ~"))
    assert bot.said() == ["🔒 aipager is locked. Send /start to unlock it."]
    with pytest.raises(ApplicationHandlerStop):
        _gate(bot, _update(callback=True))
    with pytest.raises(ApplicationHandlerStop):
        _gate(bot, _update("hi", user=999, chat=999))


def test_gate_unlocks_with_start_and_a_code(armed):
    bot = _Bot()
    armed.lock()
    with pytest.raises(ApplicationHandlerStop):
        _gate(bot, _update("/start"))
    assert bot.said()[-1] == "Send the 6-digit code from your authenticator app."
    code = idle_lock.totp_code(RFC_SECRET, int(time.time() // 30))
    with pytest.raises(ApplicationHandlerStop):
        _gate(bot, _update(code))
    assert not armed.is_locked()
    bot._app.bot.delete_message.assert_awaited_with(CHAT, 500)


def test_gate_never_takes_a_code_in_a_group(armed):
    """Even from a member of a served group: everyone there sees it."""
    raw = yaml.safe_load(scope.CONFIG_PATH.read_text())
    raw["scopes"].append({"kind": "group", "chat_id": -100, "label": "team",
                          "members": [{"id": OWNER, "label": "owner", "role": "owner"}]})
    scope.CONFIG_PATH.write_text(yaml.safe_dump(raw), encoding="utf-8")
    bot = _Bot()
    assert bot.member_in_chat(OWNER, -100) is not None
    armed.lock()
    code = idle_lock.totp_code(RFC_SECRET, int(time.time() // 30))
    with pytest.raises(ApplicationHandlerStop):
        _gate(bot, _update(code, chat=-100, chat_type="supergroup"))
    assert armed.is_locked()


def test_tick_locks_wipes_and_tells(armed, caplog):
    bot = _Bot()
    armed.note_message(CHAT, 7)
    armed.note_message(CHAT, 9)
    armed.make_due()
    with caplog.at_level("INFO", logger="aipager.bot.idle_gate"):
        _run(idle_gate.tick(bot))
    assert armed.is_locked() and not armed.wipe_pending
    bot._app.bot.delete_messages.assert_awaited_once_with(CHAT, [7, 8, 9])
    assert bot.said() == ["🔒 aipager is locked after a quiet spell. Send /start to unlock it."]
    assert f"cleared chat {CHAT}: 3 ids asked, 0 failed" in caplog.text


def test_tick_keeps_what_one_by_one_deletes_could_not_take(armed, caplog):
    bot = _Bot()
    bot._app.bot.delete_messages.side_effect = RuntimeError("bulk refused")
    bot._app.bot.delete_message.side_effect = [None, RuntimeError("gone")]
    armed.note_message(-100, 3)
    armed.note_message(-100, 4)
    armed.make_due()
    with caplog.at_level("INFO", logger="aipager.bot.idle_gate"):
        _run(idle_gate.tick(bot))
    assert "cleared chat -100: 2 ids asked, 1 failed" in caplog.text
    assert armed.wipe_plan() == {-100: [4]}


def test_lock_now_while_locked_wipes_again(armed, capsys):
    """`aipager lock now` on a locked aipager clears the chats again at
    the next tick, and says so instead of "locks within 30 s"."""
    from aipager.cli import lock as lock_cli

    bot = _Bot()
    armed.make_due()
    _run(idle_gate.tick(bot))
    armed.note_message(-100, 50)  # e.g. a message the first wipe missed
    armed.save()
    assert lock_cli.cmd_now(SimpleNamespace()) == 0
    assert "already locked" in capsys.readouterr().out
    time.sleep(0.01)
    bot._app.bot.delete_messages.reset_mock()
    _run(idle_gate.tick(bot))
    bot._app.bot.delete_messages.assert_any_await(-100, [50])
    assert not armed.wipe_pending
    _run(idle_gate.tick(bot))  # done: the next tick deletes nothing
    assert bot._app.bot.delete_messages.await_count == 1


# ---- the Mini App --------------------------------------------------------------

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _hdr(user_id):
    fields = {"auth_date": str(int(time.time())),
              "user": json.dumps({"id": user_id, "first_name": "T"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


def _status(mk_bot, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from aipager.miniapp.server import MiniAppServer
    from aipager.policy import load_policy
    from aipager.state import SessionRegistry

    monkeypatch.setattr("aipager.config.BOT_TOKEN", BOT_TOKEN)
    registry = SessionRegistry()
    bot = mk_bot(registry, scopes=scope.load_scopes(scope.CONFIG_PATH)[0])
    bot.policy = load_policy(Path("/nonexistent/policy.yaml"), Path("/nonexistent/policy.d"))
    bot._app.bot.username = "aipager_test_bot"
    srv = MiniAppServer(bot, registry, port=8765)

    async def go():
        client = TestClient(TestServer(srv._build_app()))
        await client.start_server()
        try:
            resp = await client.get("/api/status", headers=_hdr(OWNER))
            return resp.status
        finally:
            await client.close()
    return _run(go())


def test_miniapp_answers_423_while_locked(armed, mk_bot, monkeypatch):
    assert _status(mk_bot, monkeypatch) == 200
    armed.lock()
    assert _status(mk_bot, monkeypatch) == 423

"""aipager.claude_catalog: Claude Code conversations aipager does not track."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from aipager import claude_catalog, claude_jobs

SID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def config(tmp_path, monkeypatch) -> Path:
    cfg = tmp_path / "cfg"
    (cfg / "projects" / "-work-proj").mkdir(parents=True)
    (cfg / "sessions").mkdir()
    monkeypatch.setattr(claude_jobs, "config_dir", lambda: cfg)
    return cfg


def _write(cfg: Path, sid: str, records: list[dict], *, age_days: float = 0,
           project: str = "-work-proj") -> Path:
    folder = cfg / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


def _user(text, **extra) -> dict:
    return {"type": "user", "cwd": "/work/proj", "message": {"role": "user", "content": text},
            "timestamp": "2026-10-29T05:28:00.000Z", **extra}


def _sid(n: int) -> str:
    return f"{n:08d}-2222-2222-2222-222222222222"


def _registry(cfg: Path, pid: int, **fields) -> None:
    data = {"pid": pid, "sessionId": SID, "kind": "interactive", "status": "idle"}
    data.update(fields)
    (cfg / "sessions" / f"{pid}.json").write_text(json.dumps(data))


def test_first_user_message_gives_cwd_and_title(config):
    _write(config, SID, [_user("  fix   the\n  login bug  ")])
    [conv] = claude_catalog.list_foreign()
    assert (conv.session_id, conv.cwd, conv.title) == (SID, "/work/proj", "proj # fix the login bug")
    assert conv.holder_pid is None and conv.holder_status == ""


def test_title_is_cut_to_80_chars(config):
    _write(config, SID, [_user("word " * 40)])
    [conv] = claude_catalog.list_foreign()
    assert len(conv.title) == claude_catalog.TITLE_MAX and conv.title.startswith("proj # ")


def test_custom_title_beats_summary_and_user_text(config):
    _write(config, SID, [
        _user("hello there"),
        {"type": "ai-title", "aiTitle": "generated"},
        {"type": "summary", "summary": "a summary"},
        {"type": "custom-title", "customTitle": "my title"},
    ])
    [conv] = claude_catalog.list_foreign()
    assert (conv.title, conv.name) == ("proj # my title", "my title")


def test_ai_title_beats_summary_and_names_the_conversation(config):
    _write(config, SID, [_user("hello there"), {"type": "summary", "summary": "a summary"},
                         {"type": "ai-title", "aiTitle": "first-name"},
                         {"type": "ai-title", "aiTitle": "fix-login-bug"}])
    [conv] = claude_catalog.list_foreign()
    assert (conv.title, conv.name) == ("proj # fix-login-bug", "fix-login-bug")


def test_summary_beats_user_text(config):
    _write(config, SID, [_user("hello there"), {"type": "summary", "summary": "a summary"}])
    [conv] = claude_catalog.list_foreign()
    assert (conv.title, conv.name) == ("proj # a summary", "")


def test_tool_results_meta_and_wrappers_are_skipped(config):
    _write(config, SID, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "tool output"}]}},
        _user("meta text", isMeta=True),
        _user("<command-name>/clear</command-name>"),
        _user("Caveat: the messages below were generated"),
        {"type": "user", "cwd": "/work/proj", "message": {"content": [
            {"type": "text", "text": "real question"}]}},
    ])
    assert claude_catalog.list_foreign()[0].title == "proj # real question"


def test_cwd_comes_from_first_record_that_has_one(config):
    _write(config, SID, [{"type": "mode"}, _user("hi", cwd="/a"), _user("yo", cwd="/b")])
    assert claude_catalog.list_foreign()[0].cwd == "/a"


def test_no_cwd_and_no_title_read_as_empty(config):
    _write(config, SID, [{"type": "mode"}])
    [conv] = claude_catalog.list_foreign()
    assert (conv.cwd, conv.title, conv.name) == ("", "", "")


@pytest.mark.parametrize("records", [
    [{"type": "queue-operation"}, _user("review this", entrypoint="sdk-cli")],
    [{"type": "mode", "entrypoint": "sdk-py"}, _user("review this")],
    [{"type": "queue-operation"}, _user("review this")],
    [{"type": "queue-operation", "pad": "x" * claude_catalog.HEAD_READ_BYTES},
     _user("review this")],
])
def test_scripted_runs_are_left_out(config, records):
    _write(config, SID, records)
    _write(config, _sid(3), [_user("talk", entrypoint="cli")])
    assert [c.session_id for c in claude_catalog.list_foreign()] == [_sid(3)]


def test_a_queued_conversation_from_a_person_is_kept(config):
    _write(config, SID, [{"type": "queue-operation"},
                         _user("hi", entrypoint="claude-desktop")])
    assert [c.session_id for c in claude_catalog.list_foreign()] == [SID]


def test_a_name_already_in_the_form_is_not_prefixed_again(config):
    _write(config, SID, [_user("hi"), {"type": "custom-title", "customTitle": "proj # login"}])
    assert claude_catalog.list_foreign()[0].title == "proj # login"


def test_folder_alone_when_no_task(config):
    _write(config, SID, [{"type": "mode", "cwd": "/work/app/"}])
    assert claude_catalog.list_foreign()[0].title == "app"


def test_task_alone_when_no_folder(config):
    _write(config, SID, [{"type": "user", "message": {"content": "just this"}}])
    assert claude_catalog.list_foreign()[0].title == "just this"


def test_last_ts_is_last_record_timestamp_else_mtime(config):
    _write(config, SID, [_user("a"), _user("b", timestamp="2026-10-29T06:00:00.000Z")])
    other = _sid(2)
    path = _write(config, other, [_user("a"), {"type": "mode"}])
    [first, second] = claude_catalog.list_foreign()
    by_id = {c.session_id: c for c in (first, second)}
    assert by_id[SID].last_ts == 1793253600.0
    assert by_id[other].last_ts == pytest.approx(path.stat().st_mtime)


def test_head_read_is_bounded(config):
    big = "x" * (claude_catalog.HEAD_READ_BYTES + 10)
    _write(config, SID, [_user("first"), {"type": "custom-title", "customTitle": big},
                         {"type": "mode", "pad": "y" * claude_catalog.TAIL_READ_BYTES}])
    assert claude_catalog.list_foreign()[0].title == "proj # first"


def test_latest_title_is_read_from_the_tail(config):
    pad = "y" * claude_catalog.HEAD_READ_BYTES
    _write(config, SID, [_user("first"), {"type": "ai-title", "aiTitle": "early"},
                         {"type": "mode", "pad": pad}, {"type": "mode", "pad": pad},
                         {"type": "ai-title", "aiTitle": "late-task"}])
    assert claude_catalog.list_foreign()[0].title == "proj # late-task"


def test_subagent_transcripts_are_skipped(config):
    sub = config / "projects" / "-work-proj" / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / f"{_sid(3)}.jsonl").write_text(json.dumps(_user("sub")) + "\n")
    _write(config, SID, [_user("main")])
    assert [c.session_id for c in claude_catalog.list_foreign()] == [SID]


def test_continued_in_is_excluded(config):
    _write(config, SID, [_user("old"), {"type": "continued-in",
                                         "continuedInSessionId": _sid(4)}])
    assert claude_catalog.list_foreign() == []


def test_records_after_continued_in_keep_it_listed(config):
    _write(config, SID, [{"type": "continued-in", "continuedInSessionId": _sid(4)},
                         _user("resumed on purpose")])
    assert [c.session_id for c in claude_catalog.list_foreign()] == [SID]


def test_tracked_conversation_is_excluded(config, tmp_path):
    from aipager import status
    Path(status.SESSION_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(status.SESSION_STATE_FILE).write_text(json.dumps({"sessions": {
        "claude-a": {"name": "claude-a", "claude_session_id": SID}}}))
    _write(config, SID, [_user("mine")])
    _write(config, _sid(5), [_user("foreign")])
    assert [c.session_id for c in claude_catalog.list_foreign()] == [_sid(5)]


def test_day_cut(config):
    _write(config, SID, [_user("old")], age_days=15)
    _write(config, _sid(6), [_user("new")], age_days=13)
    assert [c.session_id for c in claude_catalog.list_foreign()] == [_sid(6)]
    assert len(claude_catalog.list_foreign(days=30)) == 2


def test_newest_first_and_limit(config):
    for n in range(1, 5):
        _write(config, _sid(n), [_user(f"m{n}", timestamp=f"2026-10-0{n}T00:00:00Z")],
               age_days=5 - n, project=f"-p{n % 2}")
    assert [c.session_id for c in claude_catalog.list_foreign()] == [
        _sid(4), _sid(3), _sid(2), _sid(1)]
    assert [c.session_id for c in claude_catalog.list_foreign(limit=2)] == [_sid(4), _sid(3)]


def test_invalid_stem_is_skipped(config):
    _write(config, "bad name", [_user("x")])
    _write(config, ".hidden id", [_user("x")])
    assert claude_catalog.list_foreign() == []


def test_unreadable_file_is_skipped(config, monkeypatch):
    _write(config, SID, [_user("broken")])
    _write(config, _sid(7), [_user("fine")])
    real = claude_catalog._facts

    def boom(path):
        if path.stem == SID:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(claude_catalog, "_facts", boom)
    assert [c.session_id for c in claude_catalog.list_foreign()] == [_sid(7)]


def test_missing_projects_folder_reads_as_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_jobs, "config_dir", lambda: tmp_path / "nope")
    assert claude_catalog.list_foreign() == []


def test_holder_fills_pid_and_status(config):
    _registry(config, os.getpid(), status="busy", name="work")
    _write(config, SID, [_user("held")])
    [conv] = claude_catalog.list_foreign()
    assert (conv.holder_pid, conv.holder_status) == (os.getpid(), "busy")


def test_holder_live_interactive(config):
    _registry(config, os.getpid(), status="waiting", name="work")
    assert claude_catalog.holder(SID) == {"pid": os.getpid(), "status": "waiting",
                                          "name": "work"}


def test_holder_dead_pid(config, monkeypatch):
    _registry(config, 4194000)
    monkeypatch.setattr(claude_jobs, "_pid_alive", lambda pid: False)
    assert claude_catalog.holder(SID) is None


def test_holder_proc_start_mismatch(config):
    _registry(config, os.getpid(), procStart=1)
    assert claude_catalog.holder(SID) is None


def test_holder_proc_start_match(config):
    _registry(config, os.getpid(), procStart=claude_jobs._proc_start(os.getpid()))
    assert claude_catalog.holder(SID)["pid"] == os.getpid()


def test_holder_background_kind_ignored(config):
    _registry(config, os.getpid(), kind="background")
    assert claude_catalog.holder(SID) is None


def test_holder_other_session_ignored(config):
    _registry(config, os.getpid(), sessionId=_sid(9))
    assert claude_catalog.holder(SID) is None


def test_holder_without_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_jobs, "config_dir", lambda: tmp_path / "nope")
    assert claude_catalog.holder(SID) is None

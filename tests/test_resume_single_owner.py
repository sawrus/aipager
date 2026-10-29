"""One owner per conversation: a resume closes every interactive claude
that has the conversation open (a terminal, tmux, another aipager session)
and the tmux pane it leaves, on every resume path: the command line
(``claude_jobs.prepare_resume_sync`` through ``launcher.prepare_resume``),
the bot (``_resume_target``) and ``aipager-claude`` with no daemon.

Claude's files are a fake config folder under tmp_path; signals, /proc and
tmux are the module's seams, so no real process is touched.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aipager import claude_jobs
from aipager.cli import wrap
from aipager.dtach import launcher
from aipager.state import SessionRegistry

X = "aaaaaaaa-0000-4000-8000-000000000001"
Y = "bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "claude-config"
    (root / "sessions").mkdir(parents=True)
    (root / "projects" / "-home-alice-app").mkdir(parents=True)
    (root / "projects" / "-home-alice-app" / f"{X}.jsonl").write_text('{"type":"user"}\n')
    monkeypatch.setattr(claude_jobs, "config_dir", lambda: root)
    return root


@pytest.fixture
def proc(monkeypatch):
    """Live pids, their start times and the signals sent. A signal ends a
    pid unless it is in ``ignores`` for that signal."""
    p = SimpleNamespace(start={}, alive=set(), kills=[], ignores={}, own={1})

    def kill(pid, sig):
        p.kills.append((pid, sig))
        if sig not in p.ignores.get(pid, ()):
            p.alive.discard(pid)

    monkeypatch.setattr(claude_jobs, "_proc_start", lambda pid: p.start.get(pid))
    monkeypatch.setattr(claude_jobs, "_pid_alive", lambda pid: pid in p.alive)
    monkeypatch.setattr(claude_jobs, "_kill", kill)
    monkeypatch.setattr(claude_jobs, "_sync_sleep", lambda s: None)
    monkeypatch.setattr(claude_jobs, "_own_line", lambda: set(p.own))
    return p


@pytest.fixture
def tmux(monkeypatch):
    t = SimpleNamespace(calls=[], panes={}, children=set())

    def run(sock, *args):
        t.calls.append((sock, args))
        if args[0] == "display-message":
            found = t.panes.get((sock, args[3]))
            if found is None:
                return subprocess.CompletedProcess(args, 1, "", "can't find pane")
            return subprocess.CompletedProcess(args, 0, f"{found[0]}\t{found[1]}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(claude_jobs, "_run_tmux", run)
    monkeypatch.setattr(claude_jobs, "_tmux_sockets", lambda: ["/s/default", "/s/muxel"])
    monkeypatch.setattr(claude_jobs, "_has_children", lambda pid: pid in t.children)
    return t


def reg(cfg, proc, pid, sid=X, kind="interactive", **fields):
    (cfg / "sessions" / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": sid, "kind": kind, **fields}))
    proc.alive.add(pid)


def killed(tmux):
    return [(s, a[2]) for s, a in tmux.calls if a[0] == "kill-pane"]


# ---- the command line -----------------------------------------------------------

def test_a_terminal_claude_is_closed_before_the_resume(cfg, proc, tmux):
    reg(cfg, proc, 4242)
    plan = claude_jobs.prepare_resume_sync(X)
    assert (plan.session_id, plan.action, plan.stopped_pids) == (X, "", (4242,))
    assert proc.kills == [(4242, signal.SIGTERM)]


def test_one_that_ignores_sigterm_gets_sigkill(cfg, proc, tmux):
    reg(cfg, proc, 4242)
    proc.ignores[4242] = {signal.SIGTERM}
    plan = claude_jobs.prepare_resume_sync(X)
    assert plan.stopped_pids == (4242,)
    assert proc.kills == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_one_that_outlives_both_refuses_the_resume(cfg, proc, tmux):
    reg(cfg, proc, 4242)
    proc.ignores[4242] = {signal.SIGTERM, signal.SIGKILL}
    plan = claude_jobs.prepare_resume_sync(X)
    assert (plan.action, plan.error, plan.stopped_pids) == (
        "stop_failed", "terminal_still_running", ())


@pytest.mark.parametrize("case", ["own", "other_conversation", "reused_pid", "dead"])
def test_what_is_left_alone(cfg, proc, tmux, case):
    if case == "own":
        reg(cfg, proc, 1)
    elif case == "other_conversation":
        reg(cfg, proc, 4242, sid=Y)
    elif case == "reused_pid":
        reg(cfg, proc, 4242, procStart=100)
        proc.start[4242] = 200
    else:
        reg(cfg, proc, 4242)
        proc.alive.discard(4242)
    plan = claude_jobs.prepare_resume_sync(X)
    assert plan.stopped_pids == ()
    assert proc.kills == []


def test_launcher_says_what_it_closed(cfg, proc, tmux, capsys):
    reg(cfg, proc, 4242)
    assert launcher.prepare_resume(X) == (X, 0)
    assert "Closed the claude (pid 4242)" in capsys.readouterr().out


def test_launcher_refuses_when_it_could_not_close(cfg, proc, tmux, capsys):
    reg(cfg, proc, 4242)
    proc.ignores[4242] = {signal.SIGTERM, signal.SIGKILL}
    assert launcher.prepare_resume(X) == (None, 1)
    out = capsys.readouterr()
    assert "Could not close the claude" in out.out + out.err


# ---- the tmux pane ----------------------------------------------------------------

def test_the_idle_pane_it_ran_in_is_closed(cfg, proc, tmux):
    reg(cfg, proc, 4242, tmux="work:@3.%7")
    tmux.panes[("/s/muxel", "%7")] = ("work", "900")
    claude_jobs.prepare_resume_sync(X)
    assert killed(tmux) == [("/s/muxel", "%7")]


@pytest.mark.parametrize("case", ["busy_shell", "other_session", "no_pane", "bad_field"])
def test_which_panes_stay(cfg, proc, tmux, case):
    where = "work:@3.%7"
    if case == "busy_shell":
        tmux.panes[("/s/default", "%7")] = ("work", "900")
        tmux.children.add(900)
    elif case == "other_session":
        tmux.panes[("/s/default", "%7")] = ("play", "900")
    elif case == "bad_field":
        where = "work"
    reg(cfg, proc, 4242, tmux=where)
    claude_jobs.prepare_resume_sync(X)
    assert killed(tmux) == []


# ---- the bot ------------------------------------------------------------------------

def test_the_bot_closes_the_terminal_copy(mk_bot, run_async, cfg, proc, tmux):
    reg(cfg, proc, 4242)
    bot = mk_bot(SessionRegistry())
    assert run_async(bot._resume_target(X, force=False)) == (X, None)
    assert proc.kills == [(4242, signal.SIGTERM)]


def test_a_relaunch_keeps_its_own_claude(mk_bot, run_async, cfg, proc, tmux):
    reg(cfg, proc, 4242)
    bot = mk_bot(SessionRegistry())
    assert run_async(bot._resume_target(X, force=False, own_live=True)) == (X, None)
    assert proc.kills == []


def test_the_bot_refuses_when_it_could_not_close(mk_bot, run_async, cfg, proc, tmux):
    reg(cfg, proc, 4242)
    proc.ignores[4242] = {signal.SIGTERM, signal.SIGKILL}
    bot = mk_bot(SessionRegistry())
    assert run_async(bot._resume_target(X, force=False)) == (
        X, ("bg_stop_failed", ("terminal_still_running", {})))


# ---- aipager-claude with no daemon -----------------------------------------------

def test_the_wrapper_closes_the_copy_with_no_daemon(cfg, proc, tmux, monkeypatch):
    reg(cfg, proc, 4242)
    calls = []
    monkeypatch.delenv(wrap.SESSION_ENV, raising=False)
    monkeypatch.setattr(os, "execvp", lambda f, a: calls.append(list(a)))
    monkeypatch.setattr(wrap, "_isatty", lambda: True)
    monkeypatch.setattr(wrap, "_daemon_running", lambda: False)
    monkeypatch.setattr(wrap, "_real_claude", lambda: "/real/claude")
    monkeypatch.setattr(sys, "argv", ["aipager-claude", "--resume", X])
    wrap.main()
    assert calls == [["/real/claude", "--resume", X]]
    assert proc.kills == [(4242, signal.SIGTERM)]

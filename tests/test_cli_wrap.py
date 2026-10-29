"""``aipager-claude`` (aipager.cli.wrap): the pass-through rules, the
session name, the exec, and the ``aipager wrap`` bashrc block."""

from __future__ import annotations

import argparse
import os
import stat
import sys

import pytest

from aipager.cli import wrap

REAL = "/real/bin/claude"
AIPAGER = "/venv/bin/aipager"


@pytest.fixture
def env(monkeypatch):
    """A terminal, a running daemon, no live sessions; records execvp."""
    calls: list[tuple[str, list[str]]] = []
    state = {"tty": True, "daemon": True, "live": set()}
    monkeypatch.delenv(wrap.SESSION_ENV, raising=False)
    monkeypatch.setattr(os, "execvp", lambda f, a: calls.append((f, list(a))))
    monkeypatch.setattr(wrap, "_isatty", lambda: state["tty"])
    monkeypatch.setattr(wrap, "_daemon_running", lambda: state["daemon"])
    monkeypatch.setattr(wrap, "_live_labels", lambda: set(state["live"]))
    monkeypatch.setattr(wrap, "_real_claude", lambda: REAL)
    monkeypatch.setattr(wrap, "_aipager_bin", lambda: AIPAGER)
    monkeypatch.setattr(os, "getcwd", lambda: "/work/My-Project")

    def run(*args):
        monkeypatch.setattr("sys.argv", ["aipager-claude", *args])
        calls.clear()
        wrap.main()
        assert len(calls) == 1
        return calls[0]

    run.state = state
    return run


def test_session_exec_keeps_args(env):
    assert env("--resume", "x y") == (
        AIPAGER, [AIPAGER, "session", "my_project", "--", "--resume", "x y"])


def test_session_exec_without_args(env):
    assert env()[1] == [AIPAGER, "session", "my_project", "--"]


def test_pass_through_inside_session(env, monkeypatch):
    monkeypatch.setenv(wrap.SESSION_ENV, "claude-x")
    assert env("hi") == (REAL, [REAL, "hi"])


def test_pass_through_without_tty(env):
    env.state["tty"] = False
    assert env("hi") == (REAL, [REAL, "hi"])


@pytest.mark.parametrize("flag", ["-p", "--print", "-v", "--version", "-h", "--help"])
def test_pass_through_flags(env, flag):
    assert env("a", flag) == (REAL, [REAL, "a", flag])


@pytest.mark.parametrize("sub", sorted(wrap._SUBCOMMANDS))
def test_pass_through_subcommands(env, sub):
    assert env(sub, "x") == (REAL, [REAL, sub, "x"])


def test_subcommand_name_later_in_args_is_not_a_subcommand(env):
    assert env("mcp-note")[0] == AIPAGER
    assert env("--model", "mcp")[0] == AIPAGER


def test_pass_through_when_daemon_down(env, capsys):
    env.state["daemon"] = False
    assert env("hi") == (REAL, [REAL, "hi"])
    assert capsys.readouterr().err == "aipager is not running: plain claude\n"


def test_tty_check_needs_both_streams(monkeypatch):
    class S:
        def __init__(self, v):
            self.v = v

        def isatty(self):
            return self.v

    for i, o in ((True, False), (False, True), (False, False)):
        monkeypatch.setattr("sys.stdin", S(i))
        monkeypatch.setattr("sys.stdout", S(o))
        assert wrap._isatty() is False
    monkeypatch.setattr("sys.stdin", S(True))
    monkeypatch.setattr("sys.stdout", S(True))
    assert wrap._isatty() is True


@pytest.mark.parametrize("cwd, taken, want", [
    ("/w/My-App.v2", set(), "my_appv2"),
    ("/w/proj", {"proj"}, "proj_1"),
    ("/w/proj", {"proj", "proj_1"}, "proj_2"),
    ("/w/proj", {"proj_1"}, "proj"),
    ("/w/status", set(), "c"),
    ("/w/ls", set(), "c"),
    ("/w/日本", set(), "c"),
    ("/", set(), "c"),
])
def test_session_name(cwd, taken, want):
    assert wrap.session_name(cwd, taken) == want


def test_session_name_collision_goes_through_exec(env):
    env.state["live"] = {"my_project", "my_project_1"}
    assert env()[1][2] == "my_project_2"


# --- no recursion --------------------------------------------------------

def test_launch_path_never_resolves_to_aipager_claude(tmp_path, monkeypatch):
    import importlib.util

    from aipager import claude_resolve
    # conftest blanks claude_resolve._candidate_paths; load a pristine copy.
    spec = importlib.util.spec_from_file_location(
        "_pristine_claude_resolve", claude_resolve.__file__)
    pristine = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, pristine)
    spec.loader.exec_module(pristine)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for n in ("claude", "aipager-claude"):
        f = bindir / n
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("AIPAGER_CLAUDE_BIN", raising=False)
    paths = [p for p, _ in pristine._candidate_paths()]
    assert str(bindir / "claude") in paths
    assert not any(os.path.basename(p) == "aipager-claude" for p in paths)


def test_launcher_runs_claude_by_resolved_path():
    import inspect

    from aipager.dtach import launcher
    src = inspect.getsource(launcher.launch)
    assert 'claude_bin or "claude"' in src
    assert "aipager-claude" not in src


# --- aipager wrap install | uninstall | status --------------------------

@pytest.fixture
def bashrc(tmp_path, monkeypatch):
    path = tmp_path / "rc" / ".bashrc"
    path.parent.mkdir()
    monkeypatch.setattr(wrap, "BASHRC_PATH", path)
    return path


def _run(cmd):
    return wrap.cmd_wrap(argparse.Namespace(wrap_cmd=cmd))


def test_install_creates_block(bashrc):
    assert _run("install") == 0
    assert bashrc.read_text() == wrap.BLOCK
    assert "[[ $- == *i* ]] && alias claude=aipager-claude" in wrap.BLOCK


def test_install_is_idempotent(bashrc, capsys):
    bashrc.write_text("export A=1")
    _run("install")
    once = bashrc.read_text()
    assert once.startswith("export A=1\n")
    _run("install")
    assert bashrc.read_text() == once
    assert once.count(wrap.BLOCK_START) == 1
    assert "Already installed" in capsys.readouterr().out


def test_install_keeps_mode(bashrc):
    bashrc.write_text("x\n")
    bashrc.chmod(0o600)
    _run("install")
    assert stat.S_IMODE(bashrc.stat().st_mode) == 0o600
    assert not list(bashrc.parent.glob("*.aipager-tmp"))


def test_uninstall_removes_exactly_the_block(bashrc):
    before = "export A=1\nalias ll='ls -l'\n"
    after = "export B=2\n"
    bashrc.write_text(before)
    _run("install")
    with bashrc.open("a") as f:
        f.write(after)
    _run("uninstall")
    assert bashrc.read_text() == before + after


def test_uninstall_roundtrip_restores_file(bashrc):
    bashrc.write_text("export A=1\n")
    _run("install")
    _run("uninstall")
    assert bashrc.read_text() == "export A=1\n"


def test_uninstall_when_absent_changes_nothing(bashrc, capsys):
    bashrc.write_text("export A=1\n")
    assert _run("uninstall") == 0
    assert bashrc.read_text() == "export A=1\n"
    assert "Not installed" in capsys.readouterr().out


def test_status(bashrc, capsys):
    assert _run("status") == 1
    assert "not installed" in capsys.readouterr().out
    _run("install")
    capsys.readouterr()
    assert _run("status") == 0
    out = capsys.readouterr().out
    assert "installed" in out and "not installed" not in out


def test_write_goes_through_the_test_guard(bashrc, monkeypatch):
    seen = []
    monkeypatch.setattr(wrap, "check_write", lambda p: seen.append(str(p)))
    _run("install")
    assert seen == [os.path.realpath(bashrc)]


def test_write_follows_symlink(tmp_path, monkeypatch):
    real = tmp_path / "dot" / "bashrc"
    real.parent.mkdir()
    real.write_text("a\n")
    link = tmp_path / ".bashrc"
    link.symlink_to(real)
    monkeypatch.setattr(wrap, "BASHRC_PATH", link)
    _run("install")
    assert link.is_symlink()
    assert wrap.BLOCK_START in real.read_text()


def test_parser_registered_and_bare_wrap_prints_help(monkeypatch, capsys):
    from aipager import cli
    monkeypatch.setattr("sys.argv", ["aipager", "wrap"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "install" in out and "uninstall" in out and "status" in out


def test_parser_routes_status(monkeypatch, bashrc, capsys):
    from aipager import cli
    monkeypatch.setattr("sys.argv", ["aipager", "wrap", "status"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "not installed" in capsys.readouterr().out


def test_entry_point_declared():
    import tomllib
    from pathlib import Path
    data = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["aipager-claude"] == "aipager.cli.wrap:main"


def test_real_claude_takes_the_pinned_path_first(tmp_path, monkeypatch):
    """No resolver (and its `claude --version` runs) when aipager.yaml pins
    claude_path to an executable; never the wrapper itself."""
    from aipager import claude_resolve, scope

    real = tmp_path / "claude"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    called = []
    monkeypatch.setattr(claude_resolve, "try_resolve_claude_binary",
                        lambda: called.append(1))
    monkeypatch.setattr(scope, "load_claude_path", lambda path: str(real))
    assert wrap._real_claude() == str(real)
    assert called == []
    loop = tmp_path / "aipager-claude"
    loop.write_text("#!/bin/sh\n")
    loop.chmod(0o755)
    monkeypatch.setattr(scope, "load_claude_path", lambda path: str(loop))
    assert wrap._real_claude() == "claude"
    assert called == [1]

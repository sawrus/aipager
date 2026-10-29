"""A claude aipager does not run (no ``CLAUDE_DTACH_SESSION``) calls the
hook with ``session=""``. It has no snapshot: before, every such session
shared ``claude-policy-.json``, and one session's peer turn held every
other unnamed session's terminal turns to the floor (Bash denied, reads
of ``~/.claude/**`` blocked)."""

import json

from aipager import policy_snapshot
from aipager.dtach import enforce


def _transcript(tmp_path, origin, text):
    path = tmp_path / "t.jsonl"
    entry = {"type": "user", "origin": origin,
             "message": {"role": "user", "content": text}}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return str(path)


def _call(path, tool="Bash", tool_input=None):
    return {"hook_event_name": "PreToolUse", "session": "",
            "transcript_path": path, "cwd": "/w", "tool_name": tool,
            "tool_input": tool_input or {"command": "ls"}}


def _stale_peer_snapshot():
    snap = policy_snapshot.peer_floor_snapshot()
    snap.update(joined_from_peer=True, verify_origin=True,
                joined_peers=[{"body_sha256": "ab" * 32, "status": "unverified",
                               "sender_session": None, "guard": True}])
    return snap


def test_terminal_turn_ignores_another_unnamed_sessions_snapshot(tmp_path):
    policy_snapshot.snapshot_path("").parent.mkdir(parents=True, exist_ok=True)
    policy_snapshot.snapshot_path("").write_text(
        json.dumps(_stale_peer_snapshot()), encoding="utf-8")
    path = _transcript(tmp_path, {"kind": "human"}, "run ls")
    for tool, inp in (("Bash", {"command": "ls"}),
                      ("Read", {"file_path": "~/.claude/CLAUDE.md"})):
        block, rules, info = enforce.decide_full(_call(path, tool, inp))
        assert block is None, block
        assert info["origin"] == "terminal"
        assert rules == dict(enforce.peer_auth.UNRESTRICTED_RULES)


def test_peer_turn_in_unnamed_session_is_held_but_writes_no_snapshot(tmp_path):
    origin = {"kind": "peer", "body": "do it", "verifiedPeerPid": 4242}
    path = _transcript(tmp_path, origin, "<cross-session-message>do it")
    policy_snapshot.snapshot_path("").parent.mkdir(parents=True, exist_ok=True)
    block, _rules, info = enforce.decide_full(_call(path))
    assert block is not None and info["origin"] == "peer"
    assert not policy_snapshot.snapshot_path("").exists()

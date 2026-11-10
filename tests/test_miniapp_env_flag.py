"""The Mini App is off unless ``MINIAPP_ENABLED=1`` is in the environment,
normally from ``~/.config/aipager/.env``: an ``enabled`` key in
aipager.yaml no longer turns it on, and ``aipager miniapp enable/disable``
write the switch to that ``.env``. Paths are the tmp ones
``_isolate_home_paths`` redirects to."""

from __future__ import annotations

import argparse
import os
import stat

import pytest
import yaml

from aipager import config, scope
from aipager.miniapp.cli import (
    _cmd_miniapp_disable,
    _cmd_miniapp_enable,
    _current_miniapp_config,
)


def _seed_yaml(miniapp=None):
    doc = {"schema_version": 3, "bot_token": "abc:123", "scopes": [{
        "kind": "dm", "chat_id": 42, "label": "me",
        "members": [{"id": 42, "label": "me", "role": "owner"}]}]}
    if miniapp is not None:
        doc["miniapp"] = miniapp
    scope.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    scope.CONFIG_PATH.write_text(yaml.safe_dump(doc), encoding="utf-8")


def _dotenv() -> str:
    return config.DOTENV_PATH.read_text()


@pytest.mark.parametrize("value, on", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("", False), ("no", False), ("enable", False), ("2", False),
])
def test_only_a_true_word_turns_the_switch_on(monkeypatch, value, on):
    monkeypatch.setenv("MINIAPP_ENABLED", value)
    assert config.env_flag("MINIAPP_ENABLED") is on


def test_an_unset_switch_is_off(monkeypatch):
    monkeypatch.delenv("MINIAPP_ENABLED", raising=False)
    assert config.env_flag("MINIAPP_ENABLED") is False


def test_yaml_enabled_true_is_not_read(_isolate_home_paths):
    _seed_yaml({"enabled": True, "port": 9000, "public_url": ""})
    assert scope.load_miniapp(scope.CONFIG_PATH) == {"port": 9000, "public_url": ""}
    assert _current_miniapp_config()["enabled"] is False


def test_the_daemon_switch_ignores_the_yaml(_isolate_home_paths, monkeypatch):
    _seed_yaml({"enabled": True, "port": 9000, "public_url": ""})
    monkeypatch.delenv("MINIAPP_ENABLED", raising=False)
    assert config._miniapp_enabled() is False
    monkeypatch.setenv("MINIAPP_ENABLED", "1")
    assert config._miniapp_enabled() is True


def test_the_dotenv_is_read_first_and_the_environment_wins(_isolate_home_paths,
                                                          monkeypatch):
    config.DOTENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DOTENV_PATH.write_text("# switches\nMINIAPP_ENABLED=1\nAIPAGER_T_X='a'\n")
    config._XDG_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config._XDG_CONFIG.write_text("MINIAPP_ENABLED=0\nAIPAGER_T_Y=b\n")
    monkeypatch.delenv("MINIAPP_ENABLED", raising=False)
    monkeypatch.setenv("AIPAGER_T_X", "env")
    monkeypatch.delenv("AIPAGER_T_Y", raising=False)
    config._load_env_file()
    assert os.environ["MINIAPP_ENABLED"] == "1"
    assert os.environ["AIPAGER_T_X"] == "env"
    assert os.environ["AIPAGER_T_Y"] == "b"


def test_enable_writes_the_switch_to_the_dotenv(_isolate_home_paths):
    _seed_yaml({"enabled": True, "port": 8766, "public_url": ""})
    config.DOTENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.DOTENV_PATH.write_text("# mine\nOTHER=1\n")
    config.DOTENV_PATH.chmod(0o644)
    assert _cmd_miniapp_enable(argparse.Namespace(port=None, url=None)) == 0
    assert _dotenv() == "# mine\nOTHER=1\nMINIAPP_ENABLED=1\n"
    assert stat.S_IMODE(config.DOTENV_PATH.stat().st_mode) == 0o600
    block = yaml.safe_load(scope.CONFIG_PATH.read_text())["miniapp"]
    assert block == {"port": 8766, "public_url": ""}
    assert _current_miniapp_config()["enabled"] is True


def test_disable_turns_the_switch_off(_isolate_home_paths):
    _seed_yaml()
    _cmd_miniapp_enable(argparse.Namespace(port=None, url=None))
    assert _cmd_miniapp_disable(argparse.Namespace()) == 0
    assert _dotenv() == "MINIAPP_ENABLED=0\n"
    assert _current_miniapp_config()["enabled"] is False


def test_without_a_yaml_everything_goes_to_the_dotenv(_isolate_home_paths):
    assert _cmd_miniapp_enable(
        argparse.Namespace(port=9100, url="https://t.example/")) == 0
    assert _dotenv() == ("MINIAPP_ENABLED=1\nMINIAPP_PORT=9100\n"
                         "MINIAPP_PUBLIC_URL=https://t.example/\n")
    assert not config._XDG_CONFIG.exists()
    assert _current_miniapp_config() == {
        "enabled": True, "port": 9100, "public_url": "https://t.example/"}

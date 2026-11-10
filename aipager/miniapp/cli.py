"""``aipager miniapp enable|disable|status`` — CLI toggle for the
self-hosted Mini App server (see design.md).

The switch is ``MINIAPP_ENABLED`` in ``~/.config/aipager/.env``
(``config.DOTENV_PATH``): the Mini App is off unless it says ``1``. The
port and the public URL stay in the ``miniapp:`` block of
``~/.config/aipager/aipager.yaml``, or in the same ``.env`` on an install
that has no aipager.yaml yet. It never starts or stops a listener itself:
a running daemon picks up the change only after a restart.

Deliberately NOT ``config.env``: ``migrate.retire_v1()`` renames it away on
every daemon start once aipager.yaml is authoritative.
"""

from __future__ import annotations

import argparse
import os

from aipager._test_guard import check_write
from aipager.errors import friendly_error, friendly_warn
from aipager.ui import console

_DEFAULT_PORT = 8765


def _dotenv_path():
    # Read late, not imported by value: `_isolate_home_paths` redirects
    # `aipager.config.DOTENV_PATH` for tests.
    from aipager import config
    return config.DOTENV_PATH


def _read_dotenv_lines() -> list[str]:
    try:
        return _dotenv_path().read_text().splitlines()
    except OSError:
        return []


def _parse_env_lines(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _write_dotenv(updates: dict[str, str]) -> None:
    """Read-modify-write ``~/.config/aipager/.env``: update/add only
    ``updates``' keys, leave every other line (comments, other keys)
    untouched. Mode 0600."""
    path = _dotenv_path()
    seen: set[str] = set()
    out: list[str] = []
    for line in _read_dotenv_lines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    check_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _current_miniapp_config() -> dict:
    """Snapshot read straight from disk — not from ``aipager.config``'s
    import-time cache, which reflects whatever was on disk when *this
    process* started, stale by definition for a CLI command whose whole
    job is to change that file. ``status`` deliberately doesn't require
    the daemon to be running, so this must work from a cold process too.

    ``enabled`` is the ``.env`` switch; the port and URL come from
    aipager.yaml when it exists, else from the ``.env``.
    """
    from aipager import config, scope
    env = _parse_env_lines(_read_dotenv_lines())
    enabled = env.get("MINIAPP_ENABLED", "").strip().lower() in config._TRUE_WORDS
    if scope.CONFIG_PATH.exists():
        return {"enabled": enabled, **scope.load_miniapp(scope.CONFIG_PATH)}
    try:
        port = int(env.get("MINIAPP_PORT", _DEFAULT_PORT))
    except ValueError:
        port = _DEFAULT_PORT
    return {
        "enabled": enabled,
        "port": port,
        "public_url": env.get("MINIAPP_PUBLIC_URL", ""),
    }


def _save_miniapp_config(settings: dict) -> None:
    """Write the switch to ``.env``, and the port and URL to aipager.yaml
    (or to ``.env`` when there is no aipager.yaml yet).

    `dump_miniapp` edits an existing document and will not invent one — a
    file holding only a `miniapp:` block would have no bot_token, and
    `load_scopes` rejects that outright, which would take the daemon down.
    The port and URL go first, so a failure there leaves the switch as
    it was.
    """
    from aipager import scope
    updates = {"MINIAPP_ENABLED": "1" if settings.get("enabled") else "0"}
    if scope.CONFIG_PATH.exists():
        scope.dump_miniapp(settings, scope.CONFIG_PATH)
    else:
        updates["MINIAPP_PORT"] = str(settings.get("port", _DEFAULT_PORT))
        if settings.get("public_url"):
            updates["MINIAPP_PUBLIC_URL"] = settings["public_url"]
    _write_dotenv(updates)


def _cmd_miniapp_enable(args: argparse.Namespace) -> int:
    current = _current_miniapp_config()
    port = getattr(args, "port", None) or int(current["port"])
    url = (getattr(args, "url", None) or "").strip()
    if url and not url.startswith("https://"):
        friendly_error(
            "The Mini App public URL must start with https://.",
            "  Telegram refuses non-HTTPS `web_app` buttons.",
        )
        return 2

    # Omitting --url keeps whatever override is already stored rather than
    # clearing it, so `enable --port N` doesn't silently drop the URL.
    settings = {
        "enabled": True,
        "port": port,
        "public_url": url or current["public_url"],
    }
    try:
        _save_miniapp_config(settings)
    except Exception as e:
        friendly_error(
            f"Could not write the Mini App settings: {e}",
            "  Check that ~/.config/aipager/ and its aipager.yaml can be written.",
        )
        return 2

    # Check BEFORE claiming success. This printed a green tick while
    # `aiohttp` was missing, so the daemon logged "Mini App server not
    # started" where nobody saw it and the operator believed a feature
    # was on that could never start. A tick for something that cannot
    # run is worse than an error.
    from aipager.miniapp.server import (
        miniapp_extra_available, reinstall_with_miniapp_hint,
    )
    if not miniapp_extra_available():
        friendly_warn(
            "Mini App settings saved, but this install is missing the "
            "Mini App server's own dependency - it cannot start.",
            "  aiohttp ships with aipager, so this usually means an "
            "interrupted or partial install.",
            f"  Repair it with:  {reinstall_with_miniapp_hint()}",
            "  Then restart the daemon.",
        )
        return 0

    console.print(f"[ok]✓[/ok]  Mini App enabled - port {port}")
    if settings["public_url"]:
        console.print(f"    public URL override: {settings['public_url']}")
    else:
        console.print(
            "    public URL: managed automatically - aipager starts a "
            "Cloudflare quick tunnel alongside the daemon and publishes "
            "whatever public https://*.trycloudflare.com address it is "
            "assigned"
        )
        console.print(
            "[muted]    That address is not secret and is not meant to "
            "be: every request is verified against Telegram's initData "
            "signature, not by the URL being hard to guess. The hostname "
            "stays through an update's restart, changes on any other "
            "restart, and is never written to config - "
            "if Tailscale is set up, it is used as the fallback while "
            "the tunnel comes up.[/muted]"
        )

    from aipager.wizard.daemon_io import _restart_hint
    _restart_hint()
    return 0


def _cmd_miniapp_disable(args: argparse.Namespace) -> int:
    # Keep port/public_url so re-enabling doesn't lose them.
    current = _current_miniapp_config()
    try:
        _save_miniapp_config({**current, "enabled": False})
    except Exception as e:
        friendly_error(
            f"Could not write the Mini App settings: {e}",
            "  Check that ~/.config/aipager/ and its aipager.yaml can be written.",
        )
        return 2
    console.print("[ok]✓[/ok]  Mini App disabled")

    from aipager.wizard.daemon_io import _restart_hint
    _restart_hint()
    return 0


def _cmd_miniapp_status(args: argparse.Namespace) -> int:
    from aipager.miniapp.server import miniapp_extra_available
    from aipager.miniapp.tunnel import detect_public_url

    cfg = _current_miniapp_config()

    console.print(f"enabled:        {cfg['enabled']}")
    console.print(f"port:           {cfg['port']}")
    console.print(f"manual url:     {cfg['public_url'] or '(not set)'}")
    detected = detect_public_url()
    # Labelled "fallback url" rather than "tailscale": the managed
    # Cloudflare tunnel is the normal ingress now, and an absent
    # Tailscale is not something the user is expected to go fix.
    console.print(f"fallback url:   {detected or '(none)'}")
    console.print(
        f"mini app deps:  {'installed' if miniapp_extra_available() else 'MISSING (server cannot start)'}"
    )
    if cfg["public_url"]:
        managed_note = "disabled (a manual url override is set)"
    else:
        managed_note = (
            "starts with the daemon (this command runs cold and cannot "
            "show a live URL - check the Telegram button or `aipager "
            "logs`/journalctl once the daemon is running)"
        )
    console.print(f"managed tunnel: {managed_note}")
    return 0


__all__ = [
    "_cmd_miniapp_enable",
    "_cmd_miniapp_disable",
    "_cmd_miniapp_status",
]

"""``aipager lock``: set up and manage the idle lock (:mod:`aipager.idle_lock`).

``setup`` makes the TOTP secret and prints its ``otpauth://`` URI once;
``enable`` / ``disable`` edit ``security.idle_lock`` in ``aipager.yaml``;
``now`` locks at the daemon's next tick; ``status`` shows the state.
The daemon reads the settings and the state file while it runs, so none
of these needs a restart.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time

from aipager import idle_lock


def _print_qr(uri: str) -> None:
    """A terminal QR code of *uri* when ``qrencode`` is installed."""
    qrencode = shutil.which("qrencode")
    if not qrencode:
        return
    try:
        subprocess.run([qrencode, "-t", "ansiutf8", uri], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def cmd_setup(args: argparse.Namespace) -> int:
    if idle_lock.read_secret() and not args.reset:
        print("A TOTP secret already exists; its URI was shown once, at setup.")
        print("Make a new one with `aipager lock setup --reset` (the old codes stop working).")
        return 1
    secret = idle_lock.new_secret()
    idle_lock.write_secret(secret)
    uri = idle_lock.otpauth_uri(secret)
    print("Add this to your authenticator app (shown once):")
    print(uri)
    _print_qr(uri)
    print(f"Secret saved to {idle_lock.TOTP_PATH} (mode 600).")
    print("Turn the lock on: aipager lock enable")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    from aipager.scope import dump_idle_lock, load_idle_lock

    if not idle_lock.read_secret():
        print("No TOTP secret yet: run `aipager lock setup` first.")
        return 1
    settings = load_idle_lock()
    settings["enabled"] = True
    if args.after_min is not None:
        if args.after_min < 1:
            print("--after-min must be at least 1.")
            return 2
        settings["after_min"] = args.after_min
    lock = idle_lock.LOCK
    lock.load()
    lock.touch(time.time())  # the quiet clock starts now, not at the last activity
    lock.save()
    dump_idle_lock(settings)
    print(f"Idle lock on: aipager locks after {settings['after_min']} min without activity.")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    from aipager.scope import dump_idle_lock, load_idle_lock

    settings = load_idle_lock()
    settings["enabled"] = False
    dump_idle_lock(settings)
    lock = idle_lock.LOCK
    lock.load()
    if lock.locked:
        lock.unlock(time.time())
    print("Idle lock off.")
    return 0


def cmd_now(args: argparse.Namespace) -> int:
    lock = idle_lock.LOCK
    if not lock.enabled:
        print("The idle lock is off: `aipager lock enable` first.")
        return 1
    lock.load()
    lock.make_due()
    print("aipager locks within 30 s, at the daemon's next tick.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    lock = idle_lock.LOCK
    settings = lock.settings()
    lock.load()
    last = lock.last_activity
    print("enabled:   ", "yes" if settings["enabled"] else "no")
    print("secret:    ", "set" if idle_lock.read_secret() else "not set")
    print("after:     ", settings["after_min"], "min")
    print("locked:    ", "yes" if lock.locked else "no")
    print("last seen: ", f"{int((time.time() - last) // 60)} min ago" if last else "never")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    handlers = {"setup": cmd_setup, "enable": cmd_enable, "disable": cmd_disable,
                "now": cmd_now, "status": cmd_status}
    fn = handlers.get(getattr(args, "lock_cmd", None))
    return fn(args) if fn else 0


def add_parser(sub) -> argparse.ArgumentParser:
    lock_p = sub.add_parser(
        "lock", help="the idle lock: a TOTP code after a quiet spell (setup/enable/...)")
    lock_p.set_defaults(fn=cmd_lock)
    lock_sub = lock_p.add_subparsers(dest="lock_cmd")
    setup_p = lock_sub.add_parser("setup", help="make the TOTP secret and print its URI once")
    setup_p.add_argument("--reset", action="store_true",
                         help="replace an existing secret (old codes stop working)")
    enable_p = lock_sub.add_parser("enable", help="turn the idle lock on")
    enable_p.add_argument("--after-min", type=int, default=None,
                          help="minutes without activity before it locks (default 60)")
    lock_sub.add_parser("disable", help="turn the idle lock off (and unlock)")
    lock_sub.add_parser("now", help="lock at the daemon's next tick")
    lock_sub.add_parser("status", help="show the idle lock's settings and state")
    return lock_p

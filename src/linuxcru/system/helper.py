"""The privileged part of linuxcru.

The GUI and CLI run as the normal user. Only this small program runs as root
(through pkexec or sudo), and it can only do two things: install an EDID
override for a connector, or remove one. It re-checks every input itself.

    python -m linuxcru.system.helper install --connector eDP-1 --edid FILE
    python -m linuxcru.system.helper uninstall [--connector eDP-1]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from linuxcru.system.detect import list_connectors
from linuxcru.system.install import MAX_EDID_SIZE, Installer, InstallError, Plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linuxcru-helper", description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="only show what would happen")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="install an EDID override")
    install.add_argument("--connector", required=True)
    install.add_argument("--edid", type=Path, required=True, help="EDID file to install")
    install.add_argument(
        "--replace-existing",
        action="store_true",
        help="remove an EDID override set up by hand in /etc/default/grub",
    )

    uninstall = sub.add_parser("uninstall", help="remove linuxcru's EDID override(s)")
    uninstall.add_argument("--connector", help="only this connector (default: all)")
    return parser


def _read_edid(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_EDID_SIZE + 1)
    except OSError as exc:
        raise InstallError(f"cannot read {path}: {exc.strerror}") from exc
    if len(data) > MAX_EDID_SIZE:
        raise InstallError(f"{path} is too large to be an EDID")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    real_system = args.root.resolve() == Path("/")
    if real_system and not args.dry_run and os.geteuid() != 0:
        print("linuxcru-helper must run as root (use pkexec or sudo)", file=sys.stderr)
        return 1

    installer = Installer(args.root)
    try:
        plan: Plan
        if args.command == "install":
            plan = installer.plan_install(
                args.connector,
                _read_edid(args.edid),
                known_connectors=[c.name for c in list_connectors()] if real_system else [],
                replace_existing=args.replace_existing,
            )
        else:
            plan = installer.plan_uninstall(args.connector)

        for note in plan.notes:
            print(f"note: {note}")
        if args.dry_run:
            for line in plan.describe():
                print(f"would {line}")
            return 0
        installer.apply(plan, run_commands=real_system)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""linuxcru command line.

linuxcru list                     displays and their EDIDs
linuxcru show                     detailed timings of a display
linuxcru calc 96                  work out a 96 Hz timing (changes nothing)
linuxcru generate 96 -o out.bin   write an EDID file with a 96 Hz mode
linuxcru apply 96                 install it as an override (asks for password)
linuxcru status                   what is installed and what is active
linuxcru reset                    remove linuxcru's override
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from linuxcru import __version__
from linuxcru.core.calc import Method, calculate, warnings_for
from linuxcru.core.edid import Edid, EdidError
from linuxcru.core.timing import Timing, TimingError
from linuxcru.system import privileged
from linuxcru.system.detect import (
    DRM_ROOT,
    PROC_CMDLINE,
    Connector,
    DetectError,
    active_overrides,
    default_connector,
    find_connector,
    list_connectors,
    read_cmdline,
)
from linuxcru.system.install import Installer, InstallError, firmware_name


class CliError(Exception):
    pass


# ----------------------------------------------------------------- helpers


def format_timing(t: Timing, index: int | None = None) -> str:
    head = f"{index}. " if index is not None else ""
    pol = f"{'+' if t.hsync_positive else '-'}hsync {'+' if t.vsync_positive else '-'}vsync"
    return (
        f"{head}{t.label()}  {t.pixel_clock_mhz:.2f} MHz  {t.hfreq_khz:.3f} kHz\n"
        f"     horizontal: {t.hactive} active, {t.hfront} front, {t.hsync} sync, "
        f"{t.hback} back = {t.htotal} total\n"
        f"     vertical:   {t.vactive} active, {t.vfront} front, {t.vsync} sync, "
        f"{t.vback} back = {t.vtotal} total   ({pol})"
    )


def parse_resolution(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split("x"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, got {text!r}") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("resolution must be positive")
    return width, height


def _connector(args: argparse.Namespace) -> Connector:
    try:
        if args.connector:
            return find_connector(args.connector, args.drm_root)
        return default_connector(args.drm_root)
    except DetectError as exc:
        raise CliError(str(exc)) from exc


def _source(args: argparse.Namespace) -> tuple[str, Edid]:
    """The connector name and the EDID to start from."""
    if args.edid:
        try:
            edid = Edid.from_file(args.edid)
        except (OSError, EdidError) as exc:
            raise CliError(f"cannot load {args.edid}: {exc}") from exc
        name = args.connector or _connector(args).name
        return name, edid
    connector = _connector(args)
    if not connector.edid_bytes:
        raise CliError(f"{connector.name} has no EDID (is a display connected?)")
    try:
        return connector.name, Edid.from_bytes(connector.edid_bytes)
    except EdidError as exc:
        raise CliError(f"the EDID of {connector.name} is invalid: {exc}") from exc


def _new_timing(args: argparse.Namespace, edid: Edid) -> Timing:
    native = edid.preferred_timing
    width, height = args.resolution or (None, None)
    method = args.method or (
        Method.PANEL
        if native and (width is None or (width, height) == native.resolution)
        else Method.CVT_RB2
    )
    try:
        return calculate(method, args.refresh, base=native, width=width, height=height)
    except TimingError as exc:
        raise CliError(str(exc)) from exc


def _build(args: argparse.Namespace) -> tuple[str, Edid, Edid, Timing]:
    name, edid = _source(args)
    timing = _new_timing(args, edid)
    start = edid.with_timings(edid.timings()[:1]) if args.fresh else edid
    try:
        result = start.with_timing_added(timing, preferred=args.preferred)
    except (EdidError, TimingError) as exc:
        raise CliError(str(exc)) from exc
    return name, edid, result, timing


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("not running in a terminal; pass --yes to confirm")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _run_helper(helper_args: list[str]) -> None:
    dry_run = "--dry-run" in helper_args
    try:
        result = privileged.run_helper(helper_args, escalate=not dry_run)
    except privileged.PrivilegeError as exc:
        raise CliError(str(exc)) from exc
    if privileged.password_was_cancelled(result):
        raise CliError("cancelled: administrator access was not granted")
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        raise CliError(result.stderr.strip().removeprefix("error: ") or "the helper failed")


# ---------------------------------------------------------------- commands


def cmd_list(args: argparse.Namespace) -> None:
    connectors = list_connectors(args.drm_root)
    if not connectors:
        raise CliError("no display connectors found")
    for c in connectors:
        print(f"{c.describe()}  [{c.card}]")


def cmd_show(args: argparse.Namespace) -> None:
    name, edid = _source(args)
    detailed = edid.detailed_timings()  # fails before anything is printed
    print(
        f"{name}: {edid.display_name} (EDID {edid.version[0]}.{edid.version[1]}, "
        f"{edid.extension_count} extension block(s))"
    )
    print("Detailed resolutions (the first is the preferred mode):")
    for index, dt in enumerate(detailed, start=1):
        print(f"  {format_timing(dt.timing, index)}")
    override = active_overrides(read_cmdline(args.cmdline_file)).get(name)
    if override:
        print(f"\nNote: {name} booted with the override {override}, so this is that file's EDID.")


def cmd_calc(args: argparse.Namespace) -> None:
    _, edid = _source(args)
    timing = _new_timing(args, edid)
    print(format_timing(timing))
    for note in warnings_for(timing, edid.preferred_timing):
        print(f"  ! {note}")


def cmd_generate(args: argparse.Namespace) -> None:
    name, _, result, timing = _build(args)
    output = args.output or Path(firmware_name(name))
    output.write_bytes(result.to_bytes())
    print(f"added {timing.label()} ({timing.pixel_clock_mhz:.2f} MHz)")
    print(f"wrote {output} with {len(result.timings())} detailed resolution(s)")


def cmd_apply(args: argparse.Namespace) -> None:
    name, edid, result, timing = _build(args)
    print(f"Display: {name} ({edid.display_name})")
    print("New mode:")
    print(f"  {format_timing(timing)}")
    for note in warnings_for(timing, edid.preferred_timing):
        print(f"  ! {note}")
    print("Detailed resolutions after this change:")
    for index, t in enumerate(result.timings(), start=1):
        print(f"  {index}. {t.label()}")
    if not _confirm("Install this as a boot-time EDID override?", args.yes):
        raise CliError("cancelled")

    runtime = os.environ.get("XDG_RUNTIME_DIR") or None
    with tempfile.TemporaryDirectory(dir=runtime, prefix="linuxcru-") as tmp:
        edid_file = Path(tmp) / "edid.bin"
        edid_file.write_bytes(result.to_bytes())
        helper_args = ["install", "--connector", name, "--edid", str(edid_file)]
        if args.replace_existing:
            helper_args.append("--replace-existing")
        if args.dry_run:
            helper_args.insert(0, "--dry-run")
        _run_helper(helper_args)

    if not args.dry_run:
        print("\nInstalled. Reboot, then pick the new refresh rate in")
        print("System Settings > Display & Monitor (it is already active if you used --preferred).")
        print("If the screen stays black: in the GRUB menu choose 'Advanced options' >")
        print("'recovery mode', which boots without the override, then run: linuxcru reset")


def cmd_status(args: argparse.Namespace) -> None:
    installer = Installer(args.root)
    installed = installer.installed_connectors()
    active = active_overrides(read_cmdline(args.cmdline_file))
    print("Installed by linuxcru: " + (", ".join(installed) if installed else "nothing"))
    if active:
        print("Active since boot:")
        for connector, firmware in active.items():
            print(f"  {connector or '(all connectors)'}: {firmware}")
    else:
        print("Active since boot: no EDID override")
    for name in installed:
        if active.get(name) != f"edid/{firmware_name(name)}":
            print(f"  {name}: installed but not active yet - reboot to use it")
    for path, params in installer.foreign_params().items():
        print(f"Set up outside linuxcru in {path}: {' '.join(params)}")


def cmd_reset(args: argparse.Namespace) -> None:
    installer = Installer(args.root)
    if not installer.installed_connectors():
        print("No linuxcru override is installed.")
        foreign = installer.foreign_params()
        for path, params in foreign.items():
            print(f"(Another override is configured in {path}: {' '.join(params)})")
        return
    target = args.connector or "all displays"
    if not args.dry_run and not _confirm(f"Remove the linuxcru override for {target}?", args.yes):
        raise CliError("cancelled")
    helper_args = ["uninstall"]
    if args.connector:
        helper_args += ["--connector", args.connector]
    if args.dry_run:
        helper_args.insert(0, "--dry-run")
    _run_helper(helper_args)
    if not args.dry_run:
        print("Removed. Reboot to go back to the display's own EDID.")


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linuxcru",
        description="Custom refresh rates for Linux displays through EDID overrides.",
    )
    parser.add_argument("--version", action="version", version=f"linuxcru {__version__}")
    parser.add_argument("--drm-root", type=Path, default=DRM_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--cmdline-file", type=Path, default=PROC_CMDLINE, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def source_options(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-c", "--connector", help="display connector, e.g. eDP-1 (default: built-in)"
        )
        p.add_argument("--edid", type=Path, help="start from this EDID file instead of the display")

    def mode_options(p: argparse.ArgumentParser) -> None:
        source_options(p)
        p.add_argument("refresh", type=float, help="refresh rate in Hz, e.g. 96")
        p.add_argument(
            "-m",
            "--method",
            type=Method,
            choices=list(Method),
            help="timing formula (default: panel for the native resolution, else cvt-rb2)",
        )
        p.add_argument("-r", "--resolution", type=parse_resolution, help="WIDTHxHEIGHT")

    def edit_options(p: argparse.ArgumentParser) -> None:
        mode_options(p)
        p.add_argument("--preferred", action="store_true", help="make it the default mode")
        p.add_argument(
            "--fresh",
            action="store_true",
            help="drop previously added modes and keep only the preferred one",
        )

    p = sub.add_parser("list", help="list displays")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show a display's detailed resolutions")
    source_options(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("calc", help="calculate a timing without changing anything")
    mode_options(p)
    p.set_defaults(func=cmd_calc)

    p = sub.add_parser("generate", help="write an EDID file with an added mode")
    edit_options(p)
    p.add_argument(
        "-o", "--output", type=Path, help="output file (default: linuxcru-<connector>.bin)"
    )
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("apply", help="add a mode and install the EDID override")
    edit_options(p)
    p.add_argument("--replace-existing", action="store_true", help="take over a manual override")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("status", help="show installed and active overrides")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset", help="remove linuxcru's EDID override")
    p.add_argument("-c", "--connector", help="only this connector (default: all)")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_reset)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (CliError, InstallError, EdidError, TimingError) as exc:
        print(f"linuxcru: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

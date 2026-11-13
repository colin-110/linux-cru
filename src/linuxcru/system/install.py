"""Install and remove EDID overrides (Debian/Ubuntu: GRUB + initramfs-tools).

Everything linuxcru puts on the system:

* ``/lib/firmware/edid/linuxcru-<connector>.bin``  - the EDID files
* ``/etc/initramfs-tools/hooks/linuxcru``          - copies them into the initramfs
* ``/etc/default/grub.d/99-linuxcru.cfg``          - adds ``drm.edid_firmware=...``

The GRUB drop-in only extends ``GRUB_CMDLINE_LINUX_DEFAULT``, which GRUB does
not use for "recovery mode" entries, so recovery mode always boots without the
override. Nothing else is edited unless the user asks to take over an override
set up by hand (``replace_existing``); every file that is changed or removed is
backed up first.

Work is described as a :class:`Plan` of small steps before anything happens,
which is what ``--dry-run`` prints.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from linuxcru.core.edid import Edid, EdidError
from linuxcru.system.detect import EDID_PARAMS, ambiguous_with, is_valid_connector_name

FIRMWARE_DIR = Path("lib/firmware/edid")
FIRMWARE_PREFIX = "linuxcru-"
HOOK_PATH = Path("etc/initramfs-tools/hooks/linuxcru")
INITRAMFS_TOOLS_DIR = Path("etc/initramfs-tools")
GRUB_DEFAULT = Path("etc/default/grub")
GRUB_DROPIN_DIR = Path("etc/default/grub.d")
GRUB_DROPIN = GRUB_DROPIN_DIR / "99-linuxcru.cfg"
BACKUP_DIR = Path("var/lib/linuxcru/backups")
MAX_EDID_SIZE = 32 * 1024

HOOK_SCRIPT = """\
#!/bin/sh
# Installed by linuxcru. Copies EDID override files into the initramfs so the
# graphics driver can load them early during boot. Remove with: linuxcru reset
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in
    prereqs) prereqs; exit 0 ;;
esac
. /usr/share/initramfs-tools/hook-functions
for f in /lib/firmware/edid/linuxcru-*.bin; do
    [ -e "$f" ] || continue
    copy_file firmware "$f"
done
exit 0
"""

_CMDLINE_LINE = re.compile(
    r"""^(?P<key>\s*GRUB_CMDLINE_LINUX(?:_DEFAULT)?=)(?P<q>['"]?)(?P<value>.*?)(?P=q)(?P<rest>\s*(?:\#.*)?)$"""
)


_DISABLE_RECOVERY = re.compile(r"""^\s*GRUB_DISABLE_RECOVERY=["']?(\w+)""", re.MULTILINE)


class InstallError(RuntimeError):
    pass


Runner = Callable[[Sequence[str]], None]
Which = Callable[[str], str | None]


def run_command(argv: Sequence[str]) -> None:
    result = subprocess.run(list(argv), capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise InstallError(f"{shlex.join(argv)} failed (exit {result.returncode}):\n{output}")


# ------------------------------------------------------------------- steps


@dataclass(frozen=True)
class WriteFile:
    path: Path
    content: bytes
    mode: int = 0o644

    def describe(self) -> str:
        return f"write {self.path} ({len(self.content)} bytes)"


@dataclass(frozen=True)
class RemoveFile:
    path: Path

    def describe(self) -> str:
        return f"remove {self.path}"


@dataclass(frozen=True)
class RunCommand:
    argv: tuple[str, ...]

    def describe(self) -> str:
        return f"run {shlex.join(self.argv)}"


Step = WriteFile | RemoveFile | RunCommand


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.steps

    def describe(self) -> list[str]:
        return [step.describe() for step in self.steps]


# ------------------------------------------------------------ GRUB helpers


def firmware_name(connector: str) -> str:
    return f"{FIRMWARE_PREFIX}{connector}.bin"


def kernel_param(connectors: Sequence[str]) -> str:
    """drm.edid_firmware value for our files.

    Longer names go first: the kernel uses the first entry whose name is a
    prefix of the connector, so "DP-10" must be listed before "DP-1".
    """
    ordered = sorted(connectors, key=lambda c: (-len(c), c))
    entries = [f"{c}:edid/{firmware_name(c)}" for c in ordered]
    return f"drm.edid_firmware={','.join(entries)}"


def grub_dropin(connectors: Sequence[str]) -> str:
    return (
        "# Managed by linuxcru. Do not edit; remove with: linuxcru reset\n"
        "# Only affects normal boot entries, not GRUB's recovery mode.\n"
        f'GRUB_CMDLINE_LINUX_DEFAULT="${{GRUB_CMDLINE_LINUX_DEFAULT}} {kernel_param(connectors)}"\n'
    )


def find_edid_params(text: str) -> list[str]:
    """EDID override parameters set on GRUB_CMDLINE_LINUX* lines."""
    found = []
    for line in text.splitlines():
        match = _CMDLINE_LINE.match(line)
        if not match:
            continue
        for token in match.group("value").split():
            if token.partition("=")[0] in EDID_PARAMS:
                found.append(token)
    return found


def remove_edid_params(text: str) -> tuple[str, list[str]]:
    """Remove EDID override parameters from GRUB_CMDLINE_LINUX* lines."""
    removed: list[str] = []
    lines = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        match = _CMDLINE_LINE.match(body)
        if match:
            tokens = match.group("value").split()
            keep = [t for t in tokens if t.partition("=")[0] not in EDID_PARAMS]
            if len(keep) != len(tokens):
                removed.extend(t for t in tokens if t not in keep)
                quote = match.group("q") or '"'
                body = f"{match.group('key')}{quote}{' '.join(keep)}{quote}{match.group('rest')}"
                line = body + ("\n" if line.endswith("\n") else "")
        lines.append(line)
    return "".join(lines), removed


# ---------------------------------------------------------------- installer


class Installer:
    """Plans and applies changes under ``root`` (``/`` on a real system)."""

    def __init__(
        self,
        root: Path = Path("/"),
        *,
        runner: Runner = run_command,
        which: Which = shutil.which,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.root = Path(root)
        self.runner = runner
        self.which = which
        self.clock = clock

    def path(self, relative: Path) -> Path:
        return self.root / relative

    # ---------------------------------------------------------------- state

    def check_supported(self) -> None:
        missing = []
        if not self.path(INITRAMFS_TOOLS_DIR).is_dir():
            missing.append("initramfs-tools (/etc/initramfs-tools)")
        if not self.path(GRUB_DEFAULT).is_file():
            missing.append("GRUB (/etc/default/grub)")
        if missing:
            raise InstallError(
                "this system is not supported yet; linuxcru needs "
                + " and ".join(missing)
                + ". You can still export the EDID file and install it by hand."
            )

    def installed_connectors(self) -> list[str]:
        folder = self.path(FIRMWARE_DIR)
        if not folder.is_dir():
            return []
        names = []
        for file in folder.glob(f"{FIRMWARE_PREFIX}*.bin"):
            name = file.name[len(FIRMWARE_PREFIX) : -len(".bin")]
            if is_valid_connector_name(name):
                names.append(name)
        return sorted(names)

    def foreign_params(self) -> dict[Path, list[str]]:
        """EDID overrides in GRUB config that linuxcru did not create."""
        found: dict[Path, list[str]] = {}
        candidates = [self.path(GRUB_DEFAULT)]
        dropin_dir = self.path(GRUB_DROPIN_DIR)
        if dropin_dir.is_dir():
            candidates += sorted(p for p in dropin_dir.glob("*.cfg") if p.name != GRUB_DROPIN.name)
        for file in candidates:
            if file.is_file():
                params = find_edid_params(file.read_text())
                if params:
                    found[file] = params
        return found

    def recovery_disabled(self) -> bool:
        """True if GRUB was told not to create recovery mode entries."""
        files = [self.path(GRUB_DEFAULT)]
        dropin_dir = self.path(GRUB_DROPIN_DIR)
        if dropin_dir.is_dir():
            files += sorted(dropin_dir.glob("*.cfg"))
        disabled = False
        for file in files:
            if file.is_file():
                for match in _DISABLE_RECOVERY.finditer(file.read_text()):
                    disabled = match.group(1) == "true"
        return disabled

    def update_commands(self) -> list[RunCommand]:
        grub = (
            ("update-grub",)
            if self.which("update-grub")
            else ("grub-mkconfig", "-o", "/boot/grub/grub.cfg")
        )
        return [RunCommand(("update-initramfs", "-u", "-k", "all")), RunCommand(grub)]

    # ----------------------------------------------------------------- plans

    def plan_install(
        self,
        connector: str,
        edid_bytes: bytes,
        *,
        known_connectors: Sequence[str] = (),
        replace_existing: bool = False,
    ) -> Plan:
        self.check_supported()
        if not is_valid_connector_name(connector):
            raise InstallError(f"invalid connector name {connector!r}")
        clashes = ambiguous_with(connector, list(known_connectors))
        if clashes:
            raise InstallError(
                f"an override for {connector} would also apply to {', '.join(clashes)} "
                "(the kernel matches connector names by prefix); refusing to install"
            )
        if len(edid_bytes) > MAX_EDID_SIZE:
            raise InstallError("EDID file is too large")
        try:
            edid = Edid.from_bytes(edid_bytes)
        except EdidError as exc:
            raise InstallError(f"refusing to install an invalid EDID: {exc}") from exc
        if not edid.timings():
            raise InstallError("refusing to install an EDID without any detailed timing")

        plan = Plan()
        foreign = self.foreign_params()
        if foreign:
            if not replace_existing:
                listing = "; ".join(
                    f"{p.relative_to(self.root)}: {' '.join(v)}" for p, v in foreign.items()
                )
                raise InstallError(
                    "another EDID override is already configured (" + listing + "). "
                    "Use --replace-existing to remove it from the GRUB config "
                    "(a backup is kept)."
                )
            for file in foreign:
                if file != self.path(GRUB_DEFAULT):
                    raise InstallError(f"{file} sets an EDID override; remove it by hand first")
                new_text, removed = remove_edid_params(file.read_text())
                plan.steps.append(WriteFile(file, new_text.encode()))
                plan.notes.append(f"removing {' '.join(removed)} from {file}")

        if self.recovery_disabled():
            plan.notes.append(
                "warning: GRUB recovery mode is disabled (GRUB_DISABLE_RECOVERY=true), so "
                "if the screen stays black you will have to remove the drm.edid_firmware "
                "parameter by pressing 'e' in the GRUB menu"
            )
        connectors = sorted({*self.installed_connectors(), connector})
        plan.steps.append(
            WriteFile(self.path(FIRMWARE_DIR / firmware_name(connector)), edid.to_bytes())
        )
        plan.steps.append(WriteFile(self.path(HOOK_PATH), HOOK_SCRIPT.encode(), 0o755))
        plan.steps.append(WriteFile(self.path(GRUB_DROPIN), grub_dropin(connectors).encode()))
        plan.steps.extend(self.update_commands())
        plan.notes.append(f"kernel parameter: {kernel_param(connectors)}")
        return plan

    def plan_uninstall(self, connector: str | None = None) -> Plan:
        """Remove linuxcru's override for one connector, or all of them."""
        installed = self.installed_connectors()
        if connector is not None and not is_valid_connector_name(connector):
            raise InstallError(f"invalid connector name {connector!r}")
        targets = installed if connector is None else [c for c in installed if c == connector]
        remaining = [c for c in installed if c not in targets]

        plan = Plan()
        for name in targets:
            plan.steps.append(RemoveFile(self.path(FIRMWARE_DIR / firmware_name(name))))
        if remaining:
            plan.steps.append(WriteFile(self.path(GRUB_DROPIN), grub_dropin(remaining).encode()))
        else:
            for leftover in (GRUB_DROPIN, HOOK_PATH):
                if self.path(leftover).exists():
                    plan.steps.append(RemoveFile(self.path(leftover)))
        if not plan.steps:
            plan.notes.append("nothing to remove: no linuxcru override is installed")
            return plan
        plan.steps.extend(self.update_commands())
        return plan

    # ----------------------------------------------------------------- apply

    def apply(
        self, plan: Plan, *, log: Callable[[str], None] = print, run_commands: bool = True
    ) -> Path | None:
        """Carry out a plan. Returns the backup folder, if anything was backed up."""
        backup_root = self.path(BACKUP_DIR) / self.clock().strftime("%Y%m%d-%H%M%S")
        backed_up = False
        for step in plan.steps:
            log(step.describe())
            if isinstance(step, RunCommand):
                if run_commands:
                    self.runner(step.argv)
                else:
                    log("  (skipped)")
                continue
            if step.path.exists():
                if isinstance(step, WriteFile) and step.path.read_bytes() == step.content:
                    os.chmod(step.path, step.mode)
                    continue
                self._backup(step.path, backup_root)
                backed_up = True
            if isinstance(step, WriteFile):
                self._write(step)
            elif step.path.exists():
                step.path.unlink()
        if backed_up:
            log(f"backups saved in {backup_root}")
            return backup_root
        return None

    def _backup(self, path: Path, backup_root: Path) -> None:
        target = backup_root / path.relative_to(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    @staticmethod
    def _write(step: WriteFile) -> None:
        step.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=step.path.parent, prefix=".linuxcru-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(step.content)
            os.chmod(tmp, step.mode)
            os.replace(tmp, step.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

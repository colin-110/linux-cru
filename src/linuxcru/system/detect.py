"""Find displays through sysfs and read the kernel's EDID override settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from linuxcru.core.edid import Edid, EdidError

DRM_ROOT = Path("/sys/class/drm")
PROC_CMDLINE = Path("/proc/cmdline")

# Kernel parameter names for EDID overrides (the second is from kernels < 4.15).
EDID_PARAMS = ("drm.edid_firmware", "drm_kms_helper.edid_firmware")

_CONNECTOR_DIR = re.compile(r"^(card\d+)-(.+)$")
# Kernel connector names: eDP-1, HDMI-A-1, DP-2, DVI-I-1, DP-1-1, Virtual-1 ...
CONNECTOR_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*(-[A-Za-z][A-Za-z0-9]*)*(-\d+)+$")
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")


class DetectError(RuntimeError):
    pass


def is_valid_connector_name(name: str) -> bool:
    return bool(CONNECTOR_NAME.match(name))


@dataclass(frozen=True)
class Connector:
    """A display output as the kernel sees it (e.g. eDP-1 on card2)."""

    name: str
    card: str
    path: Path
    status: str
    enabled: bool
    edid_bytes: bytes = b""
    modes: list[str] = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return self.status == "connected"

    @property
    def is_internal(self) -> bool:
        return self.name.startswith(INTERNAL_PREFIXES)

    def edid(self) -> Edid | None:
        if not self.edid_bytes:
            return None
        try:
            return Edid.from_bytes(self.edid_bytes, strict=False)
        except EdidError:
            return None

    def describe(self) -> str:
        edid = self.edid()
        name = edid.display_name if edid else "no EDID"
        kind = "built-in" if self.is_internal else "external"
        return f"{self.name} ({kind}, {self.status}): {name}"


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def list_connectors(drm_root: Path = DRM_ROOT) -> list[Connector]:
    """All connectors on all GPUs, connected ones first, then by name."""
    found: list[Connector] = []
    if not drm_root.is_dir():
        return found
    for entry in drm_root.iterdir():
        match = _CONNECTOR_DIR.match(entry.name)
        if not match or not (entry / "status").exists():
            continue
        found.append(
            Connector(
                name=match.group(2),
                card=match.group(1),
                path=entry,
                status=_read_text(entry / "status") or "unknown",
                enabled=_read_text(entry / "enabled") == "enabled",
                edid_bytes=_read_bytes(entry / "edid"),
                modes=_read_text(entry / "modes").splitlines(),
            )
        )
    found.sort(key=lambda c: (not c.connected, not c.is_internal, c.name, c.card))
    return found


def find_connector(name: str, drm_root: Path = DRM_ROOT) -> Connector:
    for connector in list_connectors(drm_root):
        if connector.name == name:
            return connector
    raise DetectError(f"no display connector named {name!r}")


def default_connector(drm_root: Path = DRM_ROOT) -> Connector:
    """The connected display to use when none is named: built-in first."""
    connected = [c for c in list_connectors(drm_root) if c.connected and c.edid_bytes]
    if not connected:
        raise DetectError("no connected display with an EDID was found")
    return connected[0]


def ambiguous_with(name: str, all_names: list[str]) -> list[str]:
    """Connectors the kernel would also match when told ``name``.

    The kernel compares only the first len(name) characters, so an override
    for "DP-1" also applies to "DP-10" or "DP-1-1".
    """
    return sorted(n for n in all_names if n != name and n.startswith(name))


# --------------------------------------------------------- kernel parameters


def read_cmdline(path: Path = PROC_CMDLINE) -> str:
    return _read_text(path)


def edid_firmware_value(cmdline: str) -> str | None:
    """Value of the last EDID override parameter on a kernel command line."""
    value = None
    for token in cmdline.split():
        key, sep, rest = token.partition("=")
        if sep and key in EDID_PARAMS:
            value = rest
    return value


def parse_edid_firmware(value: str) -> dict[str, str]:
    """Split "eDP-1:edid/a.bin,HDMI-A-1:edid/b.bin" into a mapping.

    An entry without a connector applies to every connector; it is stored
    under the empty string.
    """
    mapping: dict[str, str] = {}
    for entry in value.split(","):
        if not entry:
            continue
        connector, sep, firmware = entry.partition(":")
        if sep:
            mapping[connector] = firmware
        else:
            mapping[""] = connector
    return mapping


def kernel_firmware_for(value: str, connector: str) -> str | None:
    """Which firmware file the kernel loads for ``connector``.

    Mirrors drm_edid_load.c: the first "name:file" entry whose name is a prefix
    of the connector wins; otherwise the last entry without a name is used.
    """
    fallback = None
    for entry in value.split(","):
        name, sep, firmware = entry.partition(":")
        if sep:
            if connector[: len(name)] == name:
                return firmware
        elif entry:
            fallback = entry
    return fallback


def active_overrides(cmdline: str | None = None) -> dict[str, str]:
    """EDID overrides the running kernel was booted with."""
    value = edid_firmware_value(read_cmdline() if cmdline is None else cmdline)
    return parse_edid_firmware(value) if value else {}

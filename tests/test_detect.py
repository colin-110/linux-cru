from pathlib import Path

import pytest

from linuxcru.system.detect import (
    DetectError,
    active_overrides,
    ambiguous_with,
    default_connector,
    edid_firmware_value,
    find_connector,
    is_valid_connector_name,
    kernel_firmware_for,
    list_connectors,
    parse_edid_firmware,
)


def make_connector(
    root: Path, card: str, name: str, status: str, edid: bytes = b"", modes: str = ""
) -> None:
    path = root / f"{card}-{name}"
    path.mkdir(parents=True)
    (path / "status").write_text(status + "\n")
    (path / "enabled").write_text("enabled\n" if status == "connected" else "disabled\n")
    (path / "edid").write_bytes(edid)
    (path / "modes").write_text(modes)


@pytest.fixture
def drm(tmp_path: Path, laptop_edid_bytes: bytes) -> Path:
    root = tmp_path / "drm"
    make_connector(root, "card2", "HDMI-A-1", "disconnected")
    make_connector(root, "card2", "eDP-1", "connected", laptop_edid_bytes, "1920x1080\n1280x720\n")
    make_connector(root, "card2", "DP-1", "disconnected")
    (root / "card2").mkdir()
    (root / "renderD128").mkdir()
    (root / "version").write_text("drm 1.1.0\n")
    return root


def test_lists_connectors_connected_and_internal_first(drm: Path) -> None:
    connectors = list_connectors(drm)
    assert [c.name for c in connectors] == ["eDP-1", "DP-1", "HDMI-A-1"]
    edp = connectors[0]
    assert edp.card == "card2"
    assert edp.connected and edp.enabled and edp.is_internal
    assert edp.modes == ["1920x1080", "1280x720"]
    edid = edp.edid()
    assert edid is not None and edid.display_name == "AUO B156HAN02.1"
    assert edp.describe() == "eDP-1 (built-in, connected): AUO B156HAN02.1"
    assert connectors[2].edid() is None


def test_find_and_default_connector(drm: Path) -> None:
    assert find_connector("HDMI-A-1", drm).status == "disconnected"
    assert default_connector(drm).name == "eDP-1"
    with pytest.raises(DetectError, match="VGA-1"):
        find_connector("VGA-1", drm)


def test_missing_drm_dir(tmp_path: Path) -> None:
    assert list_connectors(tmp_path / "nope") == []
    with pytest.raises(DetectError, match="no connected display"):
        default_connector(tmp_path / "nope")


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("eDP-1", True),
        ("HDMI-A-1", True),
        ("DP-1-1", True),
        ("DVI-I-2", True),
        ("Virtual-1", True),
        ("eDP", False),
        ("eDP-1:x", False),
        ("../eDP-1", False),
        ("eDP-1,DP-1", False),
        ("", False),
    ],
)
def test_connector_name_validation(name: str, ok: bool) -> None:
    assert is_valid_connector_name(name) is ok


def test_ambiguous_names() -> None:
    names = ["DP-1", "DP-10", "DP-1-1", "DP-2", "eDP-1"]
    assert ambiguous_with("DP-1", names) == ["DP-1-1", "DP-10"]
    assert ambiguous_with("eDP-1", names) == []


CMDLINE = (
    "BOOT_IMAGE=/boot/vmlinuz root=UUID=abc ro quiet splash "
    "drm.edid_firmware=eDP-1:edid/drmcru_custom_eDP-1.bin"
)


def test_reads_override_from_cmdline() -> None:
    assert edid_firmware_value(CMDLINE) == "eDP-1:edid/drmcru_custom_eDP-1.bin"
    assert active_overrides(CMDLINE) == {"eDP-1": "edid/drmcru_custom_eDP-1.bin"}
    assert active_overrides("ro quiet") == {}
    assert edid_firmware_value("drm.edid_firmware=a.bin drm.edid_firmware=b.bin") == "b.bin"
    assert edid_firmware_value("drm_kms_helper.edid_firmware=old.bin") == "old.bin"


def test_parse_mapping_with_fallback() -> None:
    assert parse_edid_firmware("eDP-1:edid/a.bin,,edid/all.bin") == {
        "eDP-1": "edid/a.bin",
        "": "edid/all.bin",
    }


def test_kernel_lookup_uses_prefix_match() -> None:
    value = "DP-1:edid/one.bin,edid/fallback.bin"
    assert kernel_firmware_for(value, "DP-1") == "edid/one.bin"
    assert kernel_firmware_for(value, "DP-10") == "edid/one.bin"  # the kernel quirk
    assert kernel_firmware_for(value, "HDMI-A-1") == "edid/fallback.bin"
    assert kernel_firmware_for("DP-10:edid/ten.bin", "DP-1") is None

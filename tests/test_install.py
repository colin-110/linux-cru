from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from linuxcru.system import helper
from linuxcru.system.install import (
    GRUB_DROPIN,
    HOOK_PATH,
    HOOK_SCRIPT,
    Installer,
    InstallError,
    RunCommand,
    find_edid_params,
    grub_dropin,
    kernel_param,
    remove_edid_params,
)

DRMCRU_GRUB = """\
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=`( . /etc/os-release; echo ${NAME:-Ubuntu} ) 2>/dev/null || echo Ubuntu`
GRUB_CMDLINE_LINUX_DEFAULT='quiet splash drm.edid_firmware=eDP-1:edid/drmcru_custom_eDP-1.bin'
GRUB_CMDLINE_LINUX=""
"""

PLAIN_GRUB = DRMCRU_GRUB.replace(" drm.edid_firmware=eDP-1:edid/drmcru_custom_eDP-1.bin", "")


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))


def make_system(root: Path, grub: str = PLAIN_GRUB) -> Path:
    (root / "etc/initramfs-tools/hooks").mkdir(parents=True)
    (root / "etc/default").mkdir(parents=True)
    (root / "etc/default/grub").write_text(grub)
    (root / "lib/firmware/edid").mkdir(parents=True)
    return root


def installer(root: Path, runner: FakeRunner) -> Installer:
    return Installer(
        root,
        runner=runner,
        which=lambda cmd: f"/usr/sbin/{cmd}",
        clock=lambda: datetime(2026, 11, 13, 21, 0, 0),
    )


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


def test_install_writes_files_and_updates_boot(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes
) -> None:
    root = make_system(tmp_path)
    inst = installer(root, runner)
    plan = inst.plan_install("eDP-1", laptop_edid_bytes)
    assert inst.apply(plan, log=lambda _: None) is None

    assert (root / "lib/firmware/edid/linuxcru-eDP-1.bin").read_bytes() == laptop_edid_bytes
    hook = root / HOOK_PATH
    assert hook.read_text() == HOOK_SCRIPT
    assert hook.stat().st_mode & 0o777 == 0o755
    dropin = (root / GRUB_DROPIN).read_text()
    assert "drm.edid_firmware=eDP-1:edid/linuxcru-eDP-1.bin" in dropin
    assert "${GRUB_CMDLINE_LINUX_DEFAULT}" in dropin
    assert (root / "etc/default/grub").read_text() == PLAIN_GRUB
    assert runner.calls == [("update-initramfs", "-u", "-k", "all"), ("update-grub",)]
    assert inst.installed_connectors() == ["eDP-1"]


def test_reinstall_backs_up_the_old_edid(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes, base_only_bytes: bytes
) -> None:
    root = make_system(tmp_path)
    inst = installer(root, runner)
    inst.apply(inst.plan_install("eDP-1", laptop_edid_bytes), log=lambda _: None)
    backup = inst.apply(inst.plan_install("eDP-1", base_only_bytes), log=lambda _: None)

    assert backup == root / "var/lib/linuxcru/backups/20261113-210000"
    saved = backup / "lib/firmware/edid/linuxcru-eDP-1.bin"
    assert saved.read_bytes() == laptop_edid_bytes
    current = root / "lib/firmware/edid/linuxcru-eDP-1.bin"
    assert current.read_bytes() == base_only_bytes
    # Unchanged hook and drop-in are not backed up or rewritten.
    assert not (backup / HOOK_PATH).exists()


def test_existing_manual_override_blocks_install(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes
) -> None:
    root = make_system(tmp_path, DRMCRU_GRUB)
    inst = installer(root, runner)
    with pytest.raises(InstallError, match="--replace-existing"):
        inst.plan_install("eDP-1", laptop_edid_bytes)
    assert runner.calls == []


def test_replace_existing_takes_over_with_backup(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes
) -> None:
    root = make_system(tmp_path, DRMCRU_GRUB)
    inst = installer(root, runner)
    plan = inst.plan_install("eDP-1", laptop_edid_bytes, replace_existing=True)
    assert any("drmcru_custom" in note for note in plan.notes)
    backup = inst.apply(plan, log=lambda _: None)

    grub = (root / "etc/default/grub").read_text()
    assert grub == PLAIN_GRUB
    assert "GRUB_CMDLINE_LINUX_DEFAULT='quiet splash'" in grub
    assert backup is not None
    assert (backup / "etc/default/grub").read_text() == DRMCRU_GRUB
    assert inst.foreign_params() == {}


def test_override_in_other_dropin_must_be_removed_by_hand(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes
) -> None:
    root = make_system(tmp_path)
    other = root / "etc/default/grub.d/50-custom.cfg"
    other.parent.mkdir(parents=True)
    other.write_text(
        'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT drm.edid_firmware=x.bin"\n'
    )
    with pytest.raises(InstallError, match="by hand"):
        installer(root, runner).plan_install("eDP-1", laptop_edid_bytes, replace_existing=True)


@pytest.mark.parametrize(
    ("connector", "data", "message"),
    [
        ("eDP-1:evil", None, "invalid connector"),
        ("../../etc/passwd", None, "invalid connector"),
        ("eDP-1", b"not an edid", "invalid EDID"),
        ("eDP-1", b"x" * 40_000, "too large"),
    ],
)
def test_install_rejects_bad_input(
    tmp_path: Path,
    runner: FakeRunner,
    laptop_edid_bytes: bytes,
    connector: str,
    data: bytes | None,
    message: str,
) -> None:
    inst = installer(make_system(tmp_path), runner)
    with pytest.raises(InstallError, match=message):
        inst.plan_install(connector, laptop_edid_bytes if data is None else data)


def test_install_refuses_prefix_clash(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes
) -> None:
    inst = installer(make_system(tmp_path), runner)
    with pytest.raises(InstallError, match="DP-10"):
        inst.plan_install("DP-1", laptop_edid_bytes, known_connectors=["DP-1", "DP-10"])


def test_unsupported_system(tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes) -> None:
    with pytest.raises(InstallError, match="not supported"):
        installer(tmp_path, runner).plan_install("eDP-1", laptop_edid_bytes)


def test_uninstall_removes_everything(
    tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes
) -> None:
    root = make_system(tmp_path)
    inst = installer(root, runner)
    inst.apply(inst.plan_install("eDP-1", laptop_edid_bytes), log=lambda _: None)
    runner.calls.clear()

    backup = inst.apply(inst.plan_uninstall(), log=lambda _: None)
    assert inst.installed_connectors() == []
    assert not (root / GRUB_DROPIN).exists()
    assert not (root / HOOK_PATH).exists()
    assert len(runner.calls) == 2
    assert backup is not None and (backup / "lib/firmware/edid/linuxcru-eDP-1.bin").exists()


def test_uninstall_one_of_two(tmp_path: Path, runner: FakeRunner, laptop_edid_bytes: bytes) -> None:
    root = make_system(tmp_path)
    inst = installer(root, runner)
    for name in ("eDP-1", "HDMI-A-1"):
        inst.apply(inst.plan_install(name, laptop_edid_bytes), log=lambda _: None)
    assert "HDMI-A-1:edid/linuxcru-HDMI-A-1.bin,eDP-1:" in (root / GRUB_DROPIN).read_text()

    inst.apply(inst.plan_uninstall("HDMI-A-1"), log=lambda _: None)
    assert inst.installed_connectors() == ["eDP-1"]
    assert "HDMI" not in (root / GRUB_DROPIN).read_text()
    assert (root / HOOK_PATH).exists()


def test_uninstall_with_nothing_installed(tmp_path: Path, runner: FakeRunner) -> None:
    plan = installer(make_system(tmp_path), runner).plan_uninstall()
    assert plan.empty
    assert "nothing to remove" in plan.notes[0]


def test_grub_mkconfig_fallback(tmp_path: Path, laptop_edid_bytes: bytes) -> None:
    inst = Installer(make_system(tmp_path), runner=FakeRunner(), which=lambda _: None)
    assert inst.update_commands()[1] == RunCommand(("grub-mkconfig", "-o", "/boot/grub/grub.cfg"))


def test_kernel_param_orders_longer_names_first() -> None:
    assert kernel_param(["DP-1", "DP-10", "eDP-1"]) == (
        "drm.edid_firmware=DP-10:edid/linuxcru-DP-10.bin,"
        "eDP-1:edid/linuxcru-eDP-1.bin,DP-1:edid/linuxcru-DP-1.bin"
    )
    assert grub_dropin(["eDP-1"]).count("\n") == 3


@pytest.mark.parametrize(
    ("line", "expected_line"),
    [
        (
            "GRUB_CMDLINE_LINUX_DEFAULT='quiet drm.edid_firmware=a.bin splash'",
            "GRUB_CMDLINE_LINUX_DEFAULT='quiet splash'",
        ),
        (
            'GRUB_CMDLINE_LINUX="drm_kms_helper.edid_firmware=edid/x.bin"  # old',
            'GRUB_CMDLINE_LINUX=""  # old',
        ),
        ("GRUB_CMDLINE_LINUX=drm.edid_firmware=a.bin", 'GRUB_CMDLINE_LINUX=""'),
        ('GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"', 'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"'),
    ],
)
def test_remove_edid_params(line: str, expected_line: str) -> None:
    text = f"GRUB_TIMEOUT=0\n{line}\n# drm.edid_firmware=comment.bin\n"
    new_text, removed = remove_edid_params(text)
    assert new_text == f"GRUB_TIMEOUT=0\n{expected_line}\n# drm.edid_firmware=comment.bin\n"
    assert bool(removed) == (line != expected_line)
    assert find_edid_params(new_text) == []


# --------------------------------------------------------------- helper CLI


def test_helper_dry_run_changes_nothing(
    tmp_path: Path, laptop_edid_bytes: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_system(tmp_path)
    edid_file = tmp_path / "new.bin"
    edid_file.write_bytes(laptop_edid_bytes)
    args = ["--root", str(root), "--dry-run", "install", "--connector", "eDP-1"]
    assert helper.main([*args, "--edid", str(edid_file)]) == 0
    out = capsys.readouterr().out
    assert "would write" in out and "would run update-initramfs" in out
    assert not (root / "lib/firmware/edid/linuxcru-eDP-1.bin").exists()


def test_helper_install_and_uninstall_in_alternate_root(
    tmp_path: Path, laptop_edid_bytes: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_system(tmp_path)
    edid_file = tmp_path / "new.bin"
    edid_file.write_bytes(laptop_edid_bytes)
    base = ["--root", str(root)]
    assert helper.main([*base, "install", "--connector", "eDP-1", "--edid", str(edid_file)]) == 0
    assert "(skipped)" in capsys.readouterr().out  # commands never run outside "/"
    assert (root / GRUB_DROPIN).exists()
    assert helper.main([*base, "uninstall"]) == 0
    assert not (root / GRUB_DROPIN).exists()


def test_helper_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_system(tmp_path)
    args = ["--root", str(root), "install", "--connector", "eDP-1", "--edid", "/nonexistent"]
    assert helper.main(args) == 1
    assert "cannot read" in capsys.readouterr().err


def test_helper_needs_root_on_real_system(capsys: pytest.CaptureFixture[str]) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("running as root")
    assert helper.main(["uninstall"]) == 1
    assert "must run as root" in capsys.readouterr().err

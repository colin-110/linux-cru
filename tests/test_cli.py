import subprocess
from pathlib import Path

import pytest
from test_detect import make_connector
from test_install import DRMCRU_GRUB, make_system

from linuxcru.cli import main as cli
from linuxcru.core.edid import Edid

CMDLINE = "ro quiet splash drm.edid_firmware=eDP-1:edid/drmcru_custom_eDP-1.bin\n"


@pytest.fixture
def env(tmp_path: Path, laptop_edid_bytes: bytes) -> list[str]:
    drm = tmp_path / "drm"
    make_connector(drm, "card2", "eDP-1", "connected", laptop_edid_bytes)
    make_connector(drm, "card2", "HDMI-A-1", "disconnected")
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(CMDLINE)
    root = make_system(tmp_path / "root", DRMCRU_GRUB)
    return ["--drm-root", str(drm), "--cmdline-file", str(cmdline), "--root", str(root)]


def run(env: list[str], *args: str) -> int:
    return cli.main([*env, *args])


def test_list(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "eDP-1 (built-in, connected): AUO B156HAN02.1  [card2]"
    assert out[1].startswith("HDMI-A-1 (external, disconnected)")


def test_show(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "show") == 0
    out = capsys.readouterr().out
    assert "1. 1920x1080 @ 60.052 Hz  142.00 MHz" in out
    assert "2. 1920x1080 @ 96.000 Hz  237.60 MHz" in out
    assert "108 front, 48 sync, 24 back = 2100 total" in out
    assert "booted with the override edid/drmcru_custom_eDP-1.bin" in out


def test_calc_defaults_to_panel_method(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "calc", "96") == 0
    out = capsys.readouterr().out
    assert "1920x1080 @ 95.999 Hz  227.00 MHz" in out
    assert "60% above" in out


def test_calc_other_resolution_uses_cvt_rb2(
    env: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "calc", "75", "-r", "1280x720") == 0
    out = capsys.readouterr().out
    assert "1280x720 @ 7" in out and "80 sync" not in out


def test_calc_errors(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "calc", "96", "-m", "panel", "-r", "1280x720") == 1
    assert "native resolution" in capsys.readouterr().err
    assert run(env, "calc", "96", "-c", "DP-9") == 1
    assert "DP-9" in capsys.readouterr().err
    assert run(env, "calc", "96", "-c", "HDMI-A-1") == 1
    assert "no EDID" in capsys.readouterr().err


def test_bad_resolution_argument(env: list[str]) -> None:
    with pytest.raises(SystemExit):
        run(env, "calc", "96", "-r", "big")


def test_generate(env: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_file = tmp_path / "out.bin"
    assert run(env, "generate", "120", "-o", str(out_file)) == 0
    edid = Edid.from_file(out_file)
    assert [round(t.refresh_hz) for t in edid.timings()] == [60, 96, 120]
    assert "wrote" in capsys.readouterr().out


def test_generate_fresh_and_preferred(env: list[str], tmp_path: Path) -> None:
    out_file = tmp_path / "out.bin"
    assert run(env, "generate", "90", "--fresh", "--preferred", "-o", str(out_file)) == 0
    assert [round(t.refresh_hz) for t in Edid.from_file(out_file).timings()] == [90, 60]


def test_generate_from_file(env: list[str], tmp_path: Path, base_only_bytes: bytes) -> None:
    source = tmp_path / "base.bin"
    source.write_bytes(base_only_bytes)
    out_file = tmp_path / "out.bin"
    assert run(env, "generate", "96", "--edid", str(source), "-o", str(out_file)) == 0
    assert Edid.from_file(out_file).extension_count == 1


def test_apply_runs_helper(
    env: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def fake_helper(args: list[str], escalate: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        edid = Edid.from_file(args[args.index("--edid") + 1])
        assert round(edid.timings()[-1].refresh_hz) == 100
        return subprocess.CompletedProcess(args, 0, "done\n", "")

    monkeypatch.setattr(cli.privileged, "run_helper", fake_helper)
    assert run(env, "apply", "100", "--yes", "--replace-existing") == 0
    assert calls[0][:3] == ["install", "--connector", "eDP-1"]
    assert "--replace-existing" in calls[0]
    assert "Reboot" in capsys.readouterr().out


def test_apply_reports_helper_failure(
    env: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.privileged,
        "run_helper",
        lambda args, escalate=True: subprocess.CompletedProcess(
            args, 1, "", "error: another EDID override"
        ),
    )
    assert run(env, "apply", "96", "--yes") == 1
    assert "another EDID override" in capsys.readouterr().err


def test_apply_password_cancelled(
    env: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli.privileged,
        "run_helper",
        lambda args, escalate=True: subprocess.CompletedProcess(args, 126, "", ""),
    )
    assert run(env, "apply", "96", "--yes") == 1
    assert "not granted" in capsys.readouterr().err


def test_apply_without_terminal_needs_yes(
    env: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run(env, "apply", "96") == 1
    assert "--yes" in capsys.readouterr().err


def test_status(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "status") == 0
    out = capsys.readouterr().out
    assert "Installed by linuxcru: nothing" in out
    assert "eDP-1: edid/drmcru_custom_eDP-1.bin" in out
    assert "Set up outside linuxcru" in out


def test_reset_with_nothing_installed(env: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "reset", "--yes") == 0
    assert "No linuxcru override" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "linuxcru 0.1.0" in capsys.readouterr().out


def test_reset_runs_helper(
    env: list[str], monkeypatch: pytest.MonkeyPatch, laptop_edid_bytes: bytes
) -> None:
    root = Path(env[env.index("--root") + 1])
    (root / "lib/firmware/edid/linuxcru-eDP-1.bin").write_bytes(laptop_edid_bytes)
    calls: list[tuple[list[str], bool]] = []

    def fake_helper(args: list[str], escalate: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append((args, escalate))
        return subprocess.CompletedProcess(args, 0, "done\n", "")

    monkeypatch.setattr(cli.privileged, "run_helper", fake_helper)
    assert run(env, "reset", "--dry-run") == 0
    assert run(env, "reset", "-c", "eDP-1", "--yes") == 0
    assert calls == [
        (["--dry-run", "uninstall"], False),
        (["uninstall", "--connector", "eDP-1"], True),
    ]


def test_show_with_broken_edid_fails_cleanly(
    env: list[str], tmp_path: Path, laptop_edid_bytes: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_edid import corrupt_descriptor

    broken = tmp_path / "broken.bin"
    broken.write_bytes(corrupt_descriptor(laptop_edid_bytes))
    assert run(env, "show", "--edid", str(broken)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("linuxcru: ") and "offset 72" in captured.err

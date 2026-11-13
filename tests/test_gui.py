import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from test_install import DRMCRU_GRUB, make_system  # noqa: E402

from linuxcru.core.edid import Edid  # noqa: E402
from linuxcru.gui import main_window as mw  # noqa: E402
from linuxcru.gui.timing_dialog import MANUAL, TimingDialog  # noqa: E402
from linuxcru.system.detect import Connector  # noqa: E402
from linuxcru.system.install import Installer  # noqa: E402

QMessageBox = QtWidgets.QMessageBox
Yes = QMessageBox.StandardButton.Yes
CMDLINE = "ro quiet drm.edid_firmware=eDP-1:edid/drmcru_custom_eDP-1.bin"


@pytest.fixture(scope="module")
def app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def native(laptop_edid_bytes: bytes):  # type: ignore[no-untyped-def]
    return Edid.from_bytes(laptop_edid_bytes).timings()[0]


@pytest.fixture
def window(app, tmp_path: Path, laptop_edid_bytes: bytes):  # type: ignore[no-untyped-def]
    connectors = [
        Connector("eDP-1", "card2", tmp_path, "connected", True, laptop_edid_bytes),
        Connector("HDMI-A-1", "card2", tmp_path, "disconnected", False),
    ]
    root = make_system(tmp_path / "root", DRMCRU_GRUB)
    win = mw.MainWindow(connectors, Installer(root), CMDLINE)
    yield win
    win.close()


# ---------------------------------------------------------------- dialog


def test_add_dialog_uses_panel_timing(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native)
    assert dialog.method_value == "panel"
    assert not dialog.width_box.isEnabled()
    dialog.refresh.setValue(96)
    t = dialog.timing()
    assert t is not None
    assert t.pixel_clock_khz == 227_000
    assert (t.htotal, t.vtotal) == (2100, 1126)
    assert dialog.htotal.text() == "2100"
    assert "60% above" in dialog.notes.text()


def test_cvt_method_allows_other_resolutions(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native)
    dialog.method.setCurrentIndex(dialog.method.findData("cvt-rb2"))
    assert dialog.width_box.isEnabled()
    dialog.width_box.setValue(1280)
    dialog.height_box.setValue(720)
    dialog.refresh.setValue(75)
    t = dialog.timing()
    assert t is not None and t.resolution == (1280, 720)
    assert t.refresh_hz == pytest.approx(75, abs=0.05)


def test_editing_a_porch_switches_to_manual_and_keeps_refresh(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native)
    dialog.refresh.setValue(96)
    dialog.hback.setValue(100)
    assert dialog.method_value == MANUAL
    t = dialog.timing()
    assert t is not None
    assert t.hback == 100 and t.htotal == 2176
    assert t.refresh_hz == pytest.approx(96, abs=0.01)


def test_editing_pixel_clock_changes_refresh(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native, native)
    assert dialog.method_value == MANUAL
    dialog.clock.setValue(213.0)
    t = dialog.timing()
    assert t is not None and t.pixel_clock_khz == 213_000
    assert dialog.refresh.value() == pytest.approx(t.refresh_hz, abs=0.001)


def test_impossible_timing_disables_ok(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native)
    dialog.method.setCurrentIndex(dialog.method.findData("cvt"))
    dialog.width_box.setValue(3840)
    dialog.height_box.setValue(2160)
    dialog.refresh.setValue(240)
    ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    assert dialog.timing() is None
    assert "pixel clock" in dialog.notes.text()


def _type(box, text: str) -> None:  # type: ignore[no-untyped-def]
    """Replace a spin box's text by typing, without pressing Enter."""
    from PySide6.QtTest import QTest

    box.setFocus()
    box.lineEdit().selectAll()
    QTest.keyClicks(box.lineEdit(), text)


def test_typing_a_refresh_rate_recalculates_immediately(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native)
    _type(dialog.refresh, "96")
    t = dialog.timing()
    assert t is not None and t.pixel_clock_khz == 227_000
    assert dialog.clock.value() == pytest.approx(227.0)


def test_typing_a_resolution_recalculates_immediately(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native)
    dialog.method.setCurrentIndex(dialog.method.findData("cvt-rb2"))
    _type(dialog.width_box, "1280")
    _type(dialog.height_box, "720")
    t = dialog.timing()
    assert t is not None and t.resolution == (1280, 720)


def test_typing_a_porch_recalculates_immediately(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native, native)
    _type(dialog.hback, "100")
    t = dialog.timing()
    assert t is not None and t.hback == 100
    assert t.refresh_hz == pytest.approx(native.refresh_hz, abs=0.01)


def test_typing_a_pixel_clock_updates_refresh(app, native) -> None:  # type: ignore[no-untyped-def]
    dialog = TimingDialog(native, native)
    _type(dialog.clock, "227")
    t = dialog.timing()
    assert t is not None and t.pixel_clock_khz == 227_000
    assert dialog.refresh.value() == pytest.approx(96.0, abs=0.01)


# ------------------------------------------------------------ main window


def test_window_lists_modes(window) -> None:  # type: ignore[no-untyped-def]
    assert window.display.count() == 1  # displays without an EDID are hidden
    assert window.table.rowCount() == 2
    assert window.table.item(0, 1).text() == "60.052 Hz"
    assert window.table.item(1, 1).text() == "96.000 Hz"
    assert window.table.item(0, 3).text() == "preferred"
    assert "another tool" in window.status.text()
    assert not window.revert_button.isEnabled()


def test_add_move_delete_and_revert(window, native, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from linuxcru.core.calc import scale_refresh

    monkeypatch.setattr(window, "_run_dialog", lambda _t: scale_refresh(native, 120))
    window.add_mode()
    assert [round(t.refresh_hz) for t in window.timings] == [60, 96, 120]
    assert window.selected_row() == 2
    assert "not applied" in window.notes.text()

    window.move_selected(-1)
    window.move_selected(-1)
    window.move_selected(-1)  # already first: no-op
    assert [round(t.refresh_hz) for t in window.timings] == [120, 60, 96]
    assert window.table.item(0, 3).text() == "preferred"

    window.table.selectRow(2)
    window.delete_selected()
    assert [round(t.refresh_hz) for t in window.timings] == [120, 60]

    window.reload()
    assert [round(t.refresh_hz) for t in window.timings] == [60, 96]


def test_last_mode_cannot_be_deleted(window) -> None:  # type: ignore[no-untyped-def]
    window.table.selectRow(1)
    window.delete_selected()
    window.table.selectRow(0)
    assert not window.delete_button.isEnabled()
    window.delete_selected()
    assert len(window.timings) == 1


def test_export(window, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    target = tmp_path / "exported.bin"
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    window.table.selectRow(1)
    window.delete_selected()
    window.export()
    assert len(Edid.from_file(target).timings()) == 1


def test_apply_takes_over_existing_override(window, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    questions: list[str] = []
    calls: list[list[str]] = []

    def question(_parent, title, _text):  # type: ignore[no-untyped-def]
        questions.append(title)
        return Yes

    def start_helper(args, done):  # type: ignore[no-untyped-def]
        calls.append(args)
        edid = Edid.from_file(args[args.index("--edid") + 1])
        assert len(edid.timings()) == 2

    monkeypatch.setattr(mw.QMessageBox, "question", question)
    monkeypatch.setattr(window, "start_helper", start_helper)
    window.apply()
    assert questions == ["Apply EDID override", "Existing override found"]
    assert calls[0][:4] == ["install", "--connector", "eDP-1", "--replace-existing"]


def test_apply_cancelled(window, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(mw.QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    monkeypatch.setattr(window, "start_helper", lambda *a: pytest.fail("should not run"))
    window.apply()


def test_install_result_handling(window, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda _p, t, m: shown.append((t, m)))
    monkeypatch.setattr(mw.QMessageBox, "critical", lambda _p, t, m: shown.append((t, m)))
    reboot: list[str] = []
    monkeypatch.setattr(window, "ask_reboot", reboot.append)

    window._install_done(126, "", "")
    window._install_done(1, "", "error: update-grub failed")
    window._install_done(0, "done\n", "")
    assert shown == [
        ("Cancelled", "Administrator access was not granted."),
        ("Failed", "update-grub failed"),
    ]
    assert len(reboot) == 1 and "recovery mode" in reboot[0]


def test_start_helper_runs_process(window, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        mw.privileged,
        "helper_command",
        lambda args: [sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"],
    )
    results: list[tuple[int, str, str]] = []
    window.start_helper(["uninstall"], lambda c, o, e: results.append((c, o, e)))
    deadline = time.monotonic() + 10
    while not results and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    assert results and results[0][0] == 3 and results[0][1].strip() == "hello"


def test_remove_override_with_nothing_installed(window, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shown: list[str] = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda _p, t, _m: shown.append(t))
    window.remove_override()
    assert shown == ["Nothing to remove"]


def test_no_displays(app, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    win = mw.MainWindow([], Installer(tmp_path), "")
    assert "No connected display" in win.status.text()
    assert not win.apply_button.isEnabled()
    win.close()

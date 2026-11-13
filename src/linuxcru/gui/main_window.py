"""Main window: a CRU-style list of detailed resolutions for one display."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from linuxcru import __version__
from linuxcru.core.calc import warnings_for
from linuxcru.core.edid import Edid, EdidError
from linuxcru.core.timing import Timing
from linuxcru.gui.timing_dialog import TimingDialog
from linuxcru.system import privileged
from linuxcru.system.detect import Connector, active_overrides, list_connectors, read_cmdline
from linuxcru.system.install import Installer, firmware_name

HelperDone = Callable[[int, str, str], None]

RECOVERY_TEXT = (
    "If the screen stays black after rebooting, open the GRUB menu (hold Shift or "
    "press Esc while booting), choose <i>Advanced options</i> &gt; <i>recovery mode</i> "
    "(it boots without the override) and run <tt>linuxcru reset</tt>."
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        connectors: Sequence[Connector] | None = None,
        installer: Installer | None = None,
        cmdline: str | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"linuxcru {__version__}")
        self.setWindowIcon(QIcon.fromTheme("video-display"))
        self.installer = installer or Installer()
        self.cmdline = read_cmdline() if cmdline is None else cmdline
        all_connectors = list(connectors) if connectors is not None else list_connectors()
        self.connectors = [c for c in all_connectors if c.edid_bytes]

        self.edid: Edid | None = None
        self.timings: list[Timing] = []
        self.native: Timing | None = None
        self._process: QProcess | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None

        self.display = QComboBox()
        for c in self.connectors:
            self.display.addItem(c.describe(), c.name)
        self.status = QLabel()
        self.status.setWordWrap(True)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Resolution", "Refresh rate", "Pixel clock", ""])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self.edit_selected)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.add_button = QPushButton("Add…")
        self.edit_button = QPushButton("Edit…")
        self.delete_button = QPushButton("Delete")
        self.up_button = QPushButton("Move up")
        self.down_button = QPushButton("Move down")
        self.revert_button = QPushButton("Revert")
        self.add_button.clicked.connect(self.add_mode)
        self.edit_button.clicked.connect(self.edit_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.revert_button.clicked.connect(self.reload)
        side = QVBoxLayout()
        for button in (self.add_button, self.edit_button, self.delete_button):
            side.addWidget(button)
        side.addSpacing(12)
        side.addWidget(self.up_button)
        side.addWidget(self.down_button)
        side.addStretch()
        side.addWidget(self.revert_button)

        group_layout = QHBoxLayout()
        group_layout.addWidget(self.table, 1)
        group_layout.addLayout(side)
        group = QGroupBox("Detailed resolutions (the first one is the preferred mode)")
        group.setLayout(group_layout)

        self.notes = QLabel()
        self.notes.setWordWrap(True)

        self.export_button = QPushButton("Export…")
        self.reset_button = QPushButton("Remove override…")
        self.apply_button = QPushButton("Apply…")
        self.apply_button.setDefault(True)
        self.export_button.clicked.connect(self.export)
        self.reset_button.clicked.connect(self.remove_override)
        self.apply_button.clicked.connect(self.apply)
        bottom = QHBoxLayout()
        bottom.addWidget(self.export_button)
        bottom.addWidget(self.reset_button)
        bottom.addStretch()
        bottom.addWidget(self.apply_button)

        layout = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("Display:"))
        row.addWidget(self.display, 1)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(group, 1)
        layout.addWidget(self.notes)
        layout.addLayout(bottom)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(720, 460)

        self.display.currentIndexChanged.connect(self.reload)
        self.reload()

    # ------------------------------------------------------------------ data

    @property
    def connector(self) -> Connector | None:
        index = self.display.currentIndex()
        return self.connectors[index] if 0 <= index < len(self.connectors) else None

    def reload(self) -> None:
        """Load the selected display's EDID, discarding unapplied changes."""
        connector = self.connector
        self.edid = None
        self.timings = []
        self.native = None
        if connector is None:
            self.status.setText("No connected display with an EDID was found.")
        else:
            try:
                self.edid = Edid.from_bytes(connector.edid_bytes)
                self.timings = self.edid.timings()
                self.native = self.edid.preferred_timing
            except EdidError as exc:
                self.status.setText(f"The EDID of {connector.name} cannot be used: {exc}")
            else:
                self.status.setText(self._status_text(connector.name))
        self.refresh_table()

    def _status_text(self, name: str) -> str:
        active = active_overrides(self.cmdline).get(name)
        installed = name in self.installer.installed_connectors()
        ours = f"edid/{firmware_name(name)}"
        if active == ours:
            return "Running with the linuxcru override for this display."
        if installed:
            return "A linuxcru override is installed; reboot to start using it."
        if active:
            return (
                f"Running with an EDID override from another tool ({active}). "
                "The list shows the modes from that file."
            )
        return "Using the display's own EDID."

    def modified_edid(self) -> Edid:
        if self.edid is None:
            raise EdidError("no display loaded")
        return self.edid.with_timings(self.timings)

    @property
    def changed(self) -> bool:
        return self.edid is not None and self.timings != self.edid.timings()

    # ----------------------------------------------------------------- table

    def refresh_table(self, select: int | None = None) -> None:
        self.table.setRowCount(len(self.timings))
        for row, t in enumerate(self.timings):
            cells = [
                f"{t.hactive} x {t.vactive}",
                f"{t.refresh_hz:.3f} Hz",
                f"{t.pixel_clock_mhz:.2f} MHz",
                "preferred" if row == 0 else "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col in (1, 2):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, col, item)
        if select is not None and 0 <= select < len(self.timings):
            self.table.selectRow(select)
        self._update_notes()
        self._update_buttons()

    def selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _update_buttons(self) -> None:
        row = self.selected_row()
        loaded = self.edid is not None
        capacity = self.edid.timing_capacity() if self.edid else 0
        self.add_button.setEnabled(loaded and len(self.timings) < capacity)
        self.edit_button.setEnabled(row is not None)
        self.delete_button.setEnabled(row is not None and len(self.timings) > 1)
        self.up_button.setEnabled(row is not None and row > 0)
        self.down_button.setEnabled(row is not None and row < len(self.timings) - 1)
        self.revert_button.setEnabled(self.changed)
        self.export_button.setEnabled(loaded)
        self.apply_button.setEnabled(loaded)

    def _update_notes(self) -> None:
        lines: list[str] = []
        if self.changed:
            lines.append("<b>Changes are not applied yet.</b>")
        if self.native is not None:
            fastest = max(self.timings, key=lambda t: t.pixel_clock_khz, default=None)
            if fastest is not None:
                lines += warnings_for(fastest, self.native)
        self.notes.setText("<br>".join(lines))

    # ----------------------------------------------------------------- edits

    def _run_dialog(self, timing: Timing | None) -> Timing | None:
        dialog = TimingDialog(self.native, timing, self)
        if dialog.exec() != TimingDialog.DialogCode.Accepted:
            return None
        return dialog.timing()

    def add_mode(self) -> None:
        new = self._run_dialog(None)
        if new is not None:
            self.timings.append(new)
            self.refresh_table(select=len(self.timings) - 1)

    def edit_selected(self) -> None:
        row = self.selected_row()
        if row is None:
            return
        new = self._run_dialog(self.timings[row])
        if new is not None:
            self.timings[row] = new
            self.refresh_table(select=row)

    def delete_selected(self) -> None:
        row = self.selected_row()
        if row is not None and len(self.timings) > 1:
            del self.timings[row]
            self.refresh_table(select=min(row, len(self.timings) - 1))

    def move_selected(self, step: int) -> None:
        row = self.selected_row()
        if row is None or not 0 <= row + step < len(self.timings):
            return
        self.timings[row], self.timings[row + step] = self.timings[row + step], self.timings[row]
        self.refresh_table(select=row + step)

    # --------------------------------------------------------------- actions

    def export(self) -> None:
        connector = self.connector
        if connector is None or self.edid is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export EDID",
            str(Path.home() / firmware_name(connector.name)),
            "EDID files (*.bin);;All files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self.modified_edid().to_bytes())
        except (OSError, EdidError) as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def apply(self) -> None:
        connector = self.connector
        if connector is None:
            return
        try:
            edid = self.modified_edid()
        except EdidError as exc:
            QMessageBox.critical(self, "Cannot build EDID", str(exc))
            return

        modes = "".join(f"<li>{t.label()}</li>" for t in edid.timings())
        answer = QMessageBox.question(
            self,
            "Apply EDID override",
            f"<p>Install an EDID override for <b>{connector.name}</b> with these modes:</p>"
            f"<ol>{modes}</ol><p>Your password will be asked for, then the boot files are "
            f"updated (this can take a minute). The change takes effect after a reboot.</p>"
            f"<p>{RECOVERY_TEXT}</p>",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        args = ["install", "--connector", connector.name]
        try:
            foreign = self.installer.foreign_params()
        except OSError:
            foreign = {}
        if foreign:
            listing = "<br>".join(f"{p}: {' '.join(v)}" for p, v in foreign.items())
            answer = QMessageBox.question(
                self,
                "Existing override found",
                f"<p>An EDID override set up outside linuxcru was found:</p><p><tt>{listing}"
                "</tt></p><p>Remove it from the GRUB settings and use linuxcru's instead? "
                "A backup of the file is kept.</p>",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            args.append("--replace-existing")

        self._tmpdir = tempfile.TemporaryDirectory(
            dir=os.environ.get("XDG_RUNTIME_DIR") or None, prefix="linuxcru-"
        )
        edid_file = Path(self._tmpdir.name) / "edid.bin"
        edid_file.write_bytes(edid.to_bytes())
        self.start_helper([*args, "--edid", str(edid_file)], self._install_done)

    def remove_override(self) -> None:
        installed = self.installer.installed_connectors()
        if not installed:
            QMessageBox.information(self, "Nothing to remove", "No linuxcru override is installed.")
            return
        answer = QMessageBox.question(
            self,
            "Remove override",
            f"Remove the linuxcru override for {', '.join(installed)}? "
            "After a reboot the displays use their own EDID again.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_helper(["uninstall"], self._uninstall_done)

    # ---------------------------------------------------------------- helper

    def start_helper(self, args: list[str], done: HelperDone) -> None:
        """Run the root helper without freezing the window."""
        try:
            command = privileged.helper_command(args)
        except privileged.PrivilegeError as exc:
            QMessageBox.critical(self, "Cannot get administrator access", str(exc))
            return
        progress = QProgressDialog("Updating boot files…", "", 0, 0, self)
        progress.setCancelButton(None)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        process = QProcess(self)
        self._process = process

        def finished(code: int, _status: QProcess.ExitStatus) -> None:
            progress.close()
            out = bytes(process.readAllStandardOutput().data()).decode(errors="replace")
            err = bytes(process.readAllStandardError().data()).decode(errors="replace")
            self._process = None
            done(code, out, err)

        process.finished.connect(finished)
        process.start(command[0], command[1:])

    def _cleanup_tmp(self) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    def _helper_failed(self, code: int, out: str, err: str) -> bool:
        if code == 0:
            return False
        if code in (126, 127) and not out:
            QMessageBox.information(self, "Cancelled", "Administrator access was not granted.")
        else:
            message = err.strip().removeprefix("error: ") or out.strip() or f"exit code {code}"
            QMessageBox.critical(self, "Failed", message)
        return True

    def _install_done(self, code: int, out: str, err: str) -> None:
        self._cleanup_tmp()
        if self._helper_failed(code, out, err):
            return
        name = self.connector.name if self.connector else ""
        self.status.setText(self._status_text(name))
        self.ask_reboot(
            "The override is installed. Reboot now?<br><br>After rebooting, choose the new "
            "refresh rate in System Settings &gt; Display &amp; Monitor (the preferred mode "
            f"is used automatically).<br><br>{RECOVERY_TEXT}"
        )

    def _uninstall_done(self, code: int, out: str, err: str) -> None:
        if self._helper_failed(code, out, err):
            return
        self.reload()
        self.ask_reboot("The override was removed. Reboot now to go back to the display's EDID?")

    def ask_reboot(self, text: str) -> None:
        answer = QMessageBox.question(self, "Reboot", text)
        if answer == QMessageBox.StandardButton.Yes:
            QProcess.startDetached("systemctl", ["reboot"])

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cleanup_tmp()
        super().closeEvent(event)

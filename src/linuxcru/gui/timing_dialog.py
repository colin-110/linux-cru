"""The "Detailed resolution" dialog: pick a refresh rate, get a full timing."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from linuxcru.core.calc import Method, calculate, pixel_clock_for, warnings_for
from linuxcru.core.timing import MAX_PIXEL_CLOCK_KHZ, Timing, TimingError

MANUAL = "manual"


def _spin(maximum: int, minimum: int = 0) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setAlignment(Qt.AlignmentFlag.AlignRight)
    return box


class TimingDialog(QDialog):
    """Add or edit one detailed resolution.

    In an automatic mode (panel or CVT) every field is calculated from the
    resolution and refresh rate. In manual mode the porches can be edited
    and the pixel clock follows to keep the requested refresh rate, which is
    how CRU behaves.
    """

    def __init__(
        self,
        native: Timing | None,
        timing: Timing | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.native = native
        self._updating = False
        self._timing: Timing | None = None
        self.setWindowTitle("Edit detailed resolution" if timing else "Add detailed resolution")

        self.method = QComboBox()
        for m in Method:
            self.method.addItem(m.description, m.value)
        self.method.addItem("Manual", MANUAL)

        self.width_box = _spin(4095, 1)
        self.height_box = _spin(4095, 1)
        self.refresh = QDoubleSpinBox()
        self.refresh.setRange(1.0, 1000.0)
        self.refresh.setDecimals(3)
        self.refresh.setSuffix(" Hz")

        top = QFormLayout()
        top.addRow("Timing:", self.method)
        top.addRow("Width:", self.width_box)
        top.addRow("Height:", self.height_box)
        top.addRow("Refresh rate:", self.refresh)

        self.hfront, self.hsync, self.hback = _spin(1023), _spin(1023, 1), _spin(4095)
        self.vfront, self.vsync, self.vback = _spin(63), _spin(63, 1), _spin(4095)
        self.hpos = QCheckBox("positive")
        self.vpos = QCheckBox("positive")
        self.htotal = QLabel()
        self.vtotal = QLabel()
        grid = QGridLayout()
        for col, title in enumerate(("", "Horizontal", "Vertical"), start=0):
            grid.addWidget(QLabel(f"<b>{title}</b>"), 0, col)
        rows = [
            ("Front porch:", self.hfront, self.vfront),
            ("Sync width:", self.hsync, self.vsync),
            ("Back porch:", self.hback, self.vback),
            ("Total:", self.htotal, self.vtotal),
            ("Sync polarity:", self.hpos, self.vpos),
        ]
        for row, (label, h, v) in enumerate(rows, start=1):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(h, row, 1)
            grid.addWidget(v, row, 2)
        self.details = QGroupBox("Timing details")
        self.details.setLayout(grid)

        self.clock = QDoubleSpinBox()
        self.clock.setRange(0.01, MAX_PIXEL_CLOCK_KHZ / 1000)
        self.clock.setDecimals(2)
        self.clock.setSingleStep(0.01)
        self.clock.setSuffix(" MHz")
        self.summary = QLabel()
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        bottom = QFormLayout()
        bottom.addRow("Pixel clock:", self.clock)
        bottom.addRow("Result:", self.summary)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.details)
        layout.addLayout(bottom)
        layout.addWidget(self.notes)
        layout.addWidget(self.buttons)

        # Start values: the timing being edited, or the native mode for "Add".
        start = timing or native
        if timing is not None:
            self._set_method(MANUAL)
        elif native is not None:
            self._set_method(Method.PANEL.value)
        else:
            self._set_method(Method.CVT_RB2.value)
        if start is not None:
            self._show(start)
            self.refresh.setValue(round(start.refresh_hz, 3))
        else:
            self.width_box.setValue(1920)
            self.height_box.setValue(1080)
            self.refresh.setValue(60.0)
            self._recalculate()

        self.method.currentIndexChanged.connect(self._method_changed)
        for box in (self.width_box, self.height_box, self.refresh):
            box.valueChanged.connect(self._recalculate)
        for box in (self.hfront, self.hsync, self.hback, self.vfront, self.vsync, self.vback):
            box.valueChanged.connect(self._porch_changed)
        self.hpos.toggled.connect(self._porch_changed)
        self.vpos.toggled.connect(self._porch_changed)
        self.clock.valueChanged.connect(self._clock_changed)
        self._update_enabled()
        self._update_summary()

    # ----------------------------------------------------------------- state

    @property
    def method_value(self) -> str:
        return str(self.method.currentData())

    def _set_method(self, value: str) -> None:
        self.method.setCurrentIndex(self.method.findData(value))

    def _fields_timing(self, pixel_clock_khz: int) -> Timing:
        """A timing built from what the fields show."""
        base = self.native
        return Timing(
            pixel_clock_khz=pixel_clock_khz,
            hactive=self.width_box.value(),
            hfront=self.hfront.value(),
            hsync=self.hsync.value(),
            hback=self.hback.value(),
            vactive=self.height_box.value(),
            vfront=self.vfront.value(),
            vsync=self.vsync.value(),
            vback=self.vback.value(),
            hsync_positive=self.hpos.isChecked(),
            vsync_positive=self.vpos.isChecked(),
            width_mm=base.width_mm if base else 0,
            height_mm=base.height_mm if base else 0,
        )

    def _show(self, t: Timing) -> None:
        """Put a timing into the fields without triggering recalculation."""
        self._updating = True
        try:
            self.width_box.setValue(t.hactive)
            self.height_box.setValue(t.vactive)
            self.hfront.setValue(t.hfront)
            self.hsync.setValue(t.hsync)
            self.hback.setValue(t.hback)
            self.vfront.setValue(t.vfront)
            self.vsync.setValue(t.vsync)
            self.vback.setValue(t.vback)
            self.hpos.setChecked(t.hsync_positive)
            self.vpos.setChecked(t.vsync_positive)
            self.clock.setValue(t.pixel_clock_khz / 1000)
        finally:
            self._updating = False
        self._timing = t

    # --------------------------------------------------------------- reacting

    def _method_changed(self) -> None:
        if self.method_value == Method.PANEL.value and self.native is not None:
            self._updating = True
            self.width_box.setValue(self.native.hactive)
            self.height_box.setValue(self.native.vactive)
            self._updating = False
        self._update_enabled()
        self._recalculate()

    def _recalculate(self) -> None:
        if self._updating:
            return
        refresh = self.refresh.value()
        try:
            if self.method_value == MANUAL:
                draft = self._fields_timing(self.clock_khz())
                t = draft.with_pixel_clock(pixel_clock_for(draft, refresh))
            else:
                t = calculate(
                    self.method_value,
                    refresh,
                    base=self.native,
                    width=self.width_box.value(),
                    height=self.height_box.value(),
                )
        except TimingError as exc:
            self._timing = None
            self._update_summary(str(exc))
            return
        self._show(t)
        self._update_summary()

    def _porch_changed(self) -> None:
        if self._updating:
            return
        if self.method_value != MANUAL:
            self._set_method(MANUAL)  # editing a value switches to manual, like CRU
            return
        self._recalculate()

    def _clock_changed(self) -> None:
        if self._updating:
            return
        if self.method_value != MANUAL:
            self._updating = True
            self._set_method(MANUAL)
            self._updating = False
            self._update_enabled()
        t = self._fields_timing(self.clock_khz())
        self._timing = t
        self._updating = True
        self.refresh.setValue(round(t.refresh_hz, 3))
        self._updating = False
        self._update_summary()

    def clock_khz(self) -> int:
        return int(round(self.clock.value() * 100)) * 10

    def _update_enabled(self) -> None:
        panel = self.method_value == Method.PANEL.value
        self.width_box.setEnabled(not panel)
        self.height_box.setEnabled(not panel)

    def _update_summary(self, error: str | None = None) -> None:
        t = self._timing
        problems = [error] if error else (t.problems() if t else ["no timing"])
        ok = not problems
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)
        if t is not None:
            self.htotal.setText(str(t.htotal))
            self.vtotal.setText(str(t.vtotal))
            self.summary.setText(f"{t.refresh_hz:.3f} Hz, {t.hfreq_khz:.3f} kHz")
        if not ok:
            self.notes.setText(f"<span style='color:#c0392b'>{'; '.join(problems)}</span>")
        elif t is not None:
            self.notes.setText("<br>".join(warnings_for(t, self.native)))

    # ----------------------------------------------------------------- result

    def timing(self) -> Timing | None:
        """The timing the dialog currently describes, if it is valid."""
        t = self._timing
        if t is None or t.problems():
            return None
        if self.native is not None and (t.width_mm, t.height_mm) == (0, 0):
            t = replace(t, width_mm=self.native.width_mm, height_mm=self.native.height_mm)
        return t

"""Timing calculations: turn "1920x1080 at 96 Hz" into a full timing.

Methods:

* ``panel``  - keep the panel's own porches/sync/totals and raise the pixel
  clock. Laptop panels are usually happiest with this, and it is what you get
  when you just type a new refresh rate into CRU.
* ``cvt-rb2`` - VESA CVT 1.2 reduced blanking v2 (smallest blanking).
* ``cvt-rb``  - VESA CVT reduced blanking v1.
* ``cvt``     - VESA CVT with normal blanking (for CRT-style displays).

The CVT code follows the VESA formulas the same way libxcvt (the ``cvt``
command) does, so the results match that tool.
"""

from __future__ import annotations

import math
from dataclasses import replace
from enum import StrEnum

from linuxcru.core.timing import Timing, TimingError, round_pixel_clock_khz


class Method(StrEnum):
    PANEL = "panel"
    CVT_RB2 = "cvt-rb2"
    CVT_RB = "cvt-rb"
    CVT = "cvt"

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_DESCRIPTIONS = {
    Method.PANEL: "Keep panel timing, raise pixel clock (recommended for laptops)",
    Method.CVT_RB2: "CVT reduced blanking v2",
    Method.CVT_RB: "CVT reduced blanking",
    Method.CVT: "CVT standard blanking",
}

# CVT constants (VESA CVT 1.2, same names as libxcvt).
CVT_H_GRANULARITY = 8
CVT_MIN_V_PORCH = 3
CVT_MIN_V_BPORCH = 6
CVT_CLOCK_STEP_KHZ = 250
CVT_MIN_VSYNC_BP_US = 550.0
CVT_HSYNC_PERCENTAGE = 8
CVT_C_PRIME = (40 - 20) * 128 / 256 + 20
CVT_M_PRIME = 600 * 128 / 256

CVT_RB_MIN_VBLANK_US = 460.0
CVT_RB_H_SYNC = 32
CVT_RB_H_BLANK = 160
CVT_RB_V_FPORCH = 3

CVT_RB2_H_BLANK = 80
CVT_RB2_H_SYNC = 32
CVT_RB2_H_FPORCH = 8
CVT_RB2_V_SYNC = 8
CVT_RB2_V_BPORCH = 6
CVT_RB2_MIN_V_FPORCH = 1


def cvt_vsync_width(width: int, height: int) -> int:
    """CVT encodes the aspect ratio in the vertical sync width."""
    if height % 3 == 0 and height * 4 // 3 == width:
        return 4
    if height % 9 == 0 and height * 16 // 9 == width:
        return 5
    if height % 10 == 0 and height * 16 // 10 == width:
        return 6
    if (height % 4 == 0 and height * 5 // 4 == width) or (
        height % 9 == 0 and height * 15 // 9 == width
    ):
        return 7
    return 10


def _check_inputs(width: int, height: int, refresh: float) -> None:
    if width <= 0 or height <= 0:
        raise TimingError("resolution must be positive")
    if not 1 <= refresh <= 1000:
        raise TimingError(f"refresh rate {refresh} Hz is out of range (1-1000)")


def cvt(width: int, height: int, refresh: float, *, reduced: bool = False) -> Timing:
    """VESA CVT timing (normal or reduced blanking v1), progressive only."""
    _check_inputs(width, height, refresh)
    # CVT works on multiples of 8 pixels. Like the `cvt` tool, round up (so a
    # 1366 wide panel is timed as 1368) and give the spare pixels back to the
    # front porch, keeping the real active width.
    hactive = -(-width // CVT_H_GRANULARITY) * CVT_H_GRANULARITY
    vsync = cvt_vsync_width(width, height)

    if not reduced:
        hperiod = (1_000_000 / refresh - CVT_MIN_VSYNC_BP_US) / (height + CVT_MIN_V_PORCH)
        sync_and_bp = max(int(CVT_MIN_VSYNC_BP_US / hperiod) + 1, vsync + CVT_MIN_V_BPORCH)
        vfront = CVT_MIN_V_PORCH
        vback = sync_and_bp - vsync

        blank_pct = max(CVT_C_PRIME - CVT_M_PRIME * hperiod / 1000, 20.0)
        hblank = int(hactive * blank_pct / (100 - blank_pct))
        hblank -= hblank % (2 * CVT_H_GRANULARITY)
        htotal = hactive + hblank
        hsync = htotal * CVT_HSYNC_PERCENTAGE // 100
        hsync -= hsync % CVT_H_GRANULARITY
        hsync_end = hactive + hblank // 2
        hfront = hsync_end - hsync - hactive
        hback = htotal - hsync_end
        hsync_positive, vsync_positive = False, True
    else:
        hperiod = (1_000_000 / refresh - CVT_RB_MIN_VBLANK_US) / height
        vblank = max(
            int(CVT_RB_MIN_VBLANK_US / hperiod + 1),
            CVT_RB_V_FPORCH + vsync + CVT_MIN_V_BPORCH,
        )
        vfront = CVT_RB_V_FPORCH
        vback = vblank - vfront - vsync

        htotal = hactive + CVT_RB_H_BLANK
        hsync = CVT_RB_H_SYNC
        hsync_end = hactive + CVT_RB_H_BLANK // 2
        hfront = hsync_end - hsync - hactive
        hback = htotal - hsync_end
        hsync_positive, vsync_positive = True, False

    clock = int(htotal * 1000 / hperiod)
    clock -= clock % CVT_CLOCK_STEP_KHZ
    return Timing(
        pixel_clock_khz=clock,
        hactive=width,
        hfront=hfront + hactive - width,
        hsync=hsync,
        hback=hback,
        vactive=height,
        vfront=vfront,
        vsync=vsync,
        vback=vback,
        hsync_positive=hsync_positive,
        vsync_positive=vsync_positive,
    )


def cvt_rb2(width: int, height: int, refresh: float) -> Timing:
    """VESA CVT 1.2 reduced blanking version 2."""
    _check_inputs(width, height, refresh)
    hperiod = (1_000_000 / refresh - CVT_RB_MIN_VBLANK_US) / height
    min_vblank = CVT_RB2_MIN_V_FPORCH + CVT_RB2_V_SYNC + CVT_RB2_V_BPORCH
    vblank = max(math.floor(CVT_RB_MIN_VBLANK_US / hperiod) + 1, min_vblank)
    htotal = width + CVT_RB2_H_BLANK
    vtotal = height + vblank
    # RB2 uses a 1 kHz clock step; a DTD stores 10 kHz steps.
    clock = round_pixel_clock_khz(math.floor(refresh * htotal * vtotal / 1000))
    return Timing(
        pixel_clock_khz=clock,
        hactive=width,
        hfront=CVT_RB2_H_FPORCH,
        hsync=CVT_RB2_H_SYNC,
        hback=CVT_RB2_H_BLANK - CVT_RB2_H_FPORCH - CVT_RB2_H_SYNC,
        vactive=height,
        vfront=vblank - CVT_RB2_V_SYNC - CVT_RB2_V_BPORCH,
        vsync=CVT_RB2_V_SYNC,
        vback=CVT_RB2_V_BPORCH,
        hsync_positive=True,
        vsync_positive=False,
    )


def pixel_clock_for(timing: Timing, refresh: float) -> int:
    """Pixel clock (kHz, DTD-rounded) that gives ``refresh`` with these totals."""
    return round_pixel_clock_khz(timing.htotal * timing.vtotal * refresh / 1000)


def scale_refresh(base: Timing, refresh: float) -> Timing:
    """Same porches and totals as ``base``; only the pixel clock changes."""
    if not 1 <= refresh <= 1000:
        raise TimingError(f"refresh rate {refresh} Hz is out of range (1-1000)")
    return base.with_pixel_clock(pixel_clock_for(base, refresh))


def calculate(
    method: Method | str,
    refresh: float,
    *,
    base: Timing | None = None,
    width: int | None = None,
    height: int | None = None,
) -> Timing:
    """Build a timing for ``refresh`` Hz.

    ``base`` is normally the display's native timing. It is required for the
    ``panel`` method, supplies the default resolution, and its physical size
    (mm) is copied onto the result.
    """
    method = Method(method)
    if width is None or height is None:
        if base is None:
            raise TimingError("need either a base timing or a resolution")
        width = base.hactive if width is None else width
        height = base.vactive if height is None else height

    if method is Method.PANEL:
        if base is None:
            raise TimingError("the panel method needs the display's native timing")
        if (width, height) != base.resolution:
            raise TimingError(
                f"the panel method keeps the native resolution "
                f"{base.hactive}x{base.vactive}; use a CVT method for {width}x{height}"
            )
        result = scale_refresh(base, refresh)
    elif method is Method.CVT_RB2:
        result = cvt_rb2(width, height, refresh)
    else:
        result = cvt(width, height, refresh, reduced=method is Method.CVT_RB)

    if base is not None and method is not Method.PANEL:
        result = replace(result, width_mm=base.width_mm, height_mm=base.height_mm)
    result.validate()
    return result


def warnings_for(timing: Timing, native: Timing | None) -> list[str]:
    """Plain-language cautions about a timing compared with the native one."""
    notes: list[str] = []
    if native is None:
        return notes
    ratio = timing.pixel_clock_khz / native.pixel_clock_khz
    if ratio > 1.0:
        notes.append(
            f"Pixel clock is {(ratio - 1) * 100:.0f}% above the panel's native mode "
            f"({native.pixel_clock_mhz:.2f} -> {timing.pixel_clock_mhz:.2f} MHz)."
        )
    if timing.refresh_hz > native.refresh_hz * 2:
        notes.append("More than double the native refresh rate; most panels will not sync.")
    if timing.resolution != native.resolution:
        notes.append("Not the native resolution, so the image will be scaled.")
    return notes

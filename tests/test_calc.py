import shutil
import subprocess

import pytest

from linuxcru.core.calc import (
    Method,
    calculate,
    cvt,
    cvt_rb2,
    cvt_vsync_width,
    scale_refresh,
    warnings_for,
)
from linuxcru.core.timing import Timing, TimingError

NATIVE = Timing(
    pixel_clock_khz=142_000,
    hactive=1920,
    hfront=108,
    hsync=48,
    hback=24,
    vactive=1080,
    vfront=10,
    vsync=10,
    vback=26,
    hsync_positive=False,
    vsync_positive=False,
    width_mm=344,
    height_mm=193,
)


def porches(t: Timing) -> tuple[int, ...]:
    return (t.pixel_clock_khz, t.hfront, t.hsync, t.hback, t.vfront, t.vsync, t.vback)


def test_panel_method_keeps_totals_and_raises_clock() -> None:
    t = scale_refresh(NATIVE, 96)
    assert (t.htotal, t.vtotal) == (NATIVE.htotal, NATIVE.vtotal)
    assert t.pixel_clock_khz == 227_000
    assert t.refresh_hz == pytest.approx(96, abs=0.01)
    assert t.hsync_positive == NATIVE.hsync_positive
    assert (t.width_mm, t.height_mm) == (344, 193)


# Expected values below come from the `cvt` tool (libxcvt) and the VESA DMT list.
@pytest.mark.parametrize(
    ("refresh", "reduced", "expected"),
    [
        (60, False, (173_000, 128, 200, 328, 3, 5, 32)),
        (96, False, (288_000, 144, 208, 352, 3, 5, 56)),
        (60, True, (138_500, 48, 32, 80, 3, 5, 23)),
        (120, True, (285_250, 48, 32, 80, 3, 5, 56)),
    ],
)
def test_cvt_matches_reference(refresh: int, reduced: bool, expected: tuple[int, ...]) -> None:
    assert porches(cvt(1920, 1080, refresh, reduced=reduced)) == expected


def test_cvt_polarities() -> None:
    normal = cvt(1920, 1080, 60)
    assert (normal.hsync_positive, normal.vsync_positive) == (False, True)
    reduced = cvt(1920, 1080, 60, reduced=True)
    assert (reduced.hsync_positive, reduced.vsync_positive) == (True, False)


def test_cvt_rb2_1080p60() -> None:
    t = cvt_rb2(1920, 1080, 60)
    assert t.pixel_clock_khz == 133_320
    assert (t.htotal, t.vtotal) == (2000, 1111)
    assert (t.hfront, t.hsync, t.hback) == (8, 32, 40)
    assert (t.vsync, t.vback) == (8, 6)


def test_cvt_rb2_96hz_is_close_to_target() -> None:
    t = cvt_rb2(1920, 1080, 96)
    assert t.refresh_hz == pytest.approx(96, abs=0.05)
    assert t.vfront >= 1


@pytest.mark.parametrize(
    ("size", "vsync"),
    [((1024, 768), 4), ((1920, 1080), 5), ((1920, 1200), 6), ((1280, 1024), 7), ((1366, 768), 10)],
)
def test_vsync_width_encodes_aspect_ratio(size: tuple[int, int], vsync: int) -> None:
    assert cvt_vsync_width(*size) == vsync


def test_calculate_dispatches_and_copies_size() -> None:
    assert calculate("panel", 96, base=NATIVE).pixel_clock_khz == 227_000
    t = calculate(Method.CVT_RB2, 96, base=NATIVE)
    assert (t.width_mm, t.height_mm) == (344, 193)
    assert calculate(Method.CVT, 60, width=1280, height=720).resolution == (1280, 720)


def test_calculate_rejects_bad_requests() -> None:
    with pytest.raises(TimingError, match="native resolution"):
        calculate(Method.PANEL, 96, base=NATIVE, width=1280, height=720)
    with pytest.raises(TimingError, match="base timing or a resolution"):
        calculate(Method.CVT, 96)
    with pytest.raises(TimingError, match="out of range"):
        calculate(Method.PANEL, 0, base=NATIVE)
    with pytest.raises(ValueError):
        calculate("magic", 96, base=NATIVE)


def test_calculate_rejects_clock_too_high_for_a_dtd() -> None:
    with pytest.raises(TimingError, match="pixel clock"):
        calculate(Method.CVT, 240, width=3840, height=2160)


def test_warnings() -> None:
    notes = warnings_for(scale_refresh(NATIVE, 96), NATIVE)
    assert any("60% above" in n for n in notes)
    assert any("double" in n for n in warnings_for(scale_refresh(NATIVE, 144), NATIVE))
    assert warnings_for(NATIVE, NATIVE) == []
    assert warnings_for(NATIVE, None) == []


def _cvt_tool(width: int, height: int, refresh: int, reduced: bool) -> tuple[int, ...]:
    args = ["cvt", *(["-r"] if reduced else []), str(width), str(height), str(refresh)]
    line = subprocess.run(args, capture_output=True, text=True, check=True).stdout.splitlines()[1]
    fields = line.split()
    clock = round(float(fields[2]) * 1000)
    h = [int(x) for x in fields[3:7]]
    v = [int(x) for x in fields[7:11]]
    return (clock, h[1] - h[0], h[2] - h[1], h[3] - h[2], v[1] - v[0], v[2] - v[1], v[3] - v[2])


@pytest.mark.skipif(shutil.which("cvt") is None, reason="cvt tool not installed")
@pytest.mark.parametrize(
    ("width", "height", "refresh", "reduced"),
    [
        (w, h, r, red)
        for (w, h) in [(1920, 1080), (2560, 1440), (1280, 1024), (1366, 768), (1920, 1200)]
        for r, red in [(60, False), (75, False), (96, False), (144, False), (60, True), (120, True)]
    ],
)
def test_cvt_matches_system_cvt_tool(width: int, height: int, refresh: int, reduced: bool) -> None:
    ours = cvt(width, height, refresh, reduced=reduced)
    clock, hfront, hsync, hback, vfront, vsync, vback = _cvt_tool(width, height, refresh, reduced)
    assert ours.pixel_clock_khz == clock
    assert (ours.vfront, ours.vsync, ours.vback) == (vfront, vsync, vback)
    assert (ours.hsync, ours.hback) == (hsync, hback)
    # The tool rounds the width up to a multiple of 8; we keep the real width
    # and put the spare pixels in the front porch, so the totals still match.
    assert ours.hactive == width
    assert ours.hfront == hfront + (-width % 8)


def test_cvt_keeps_odd_widths() -> None:
    t = cvt(1366, 768, 60)
    assert t.hactive == 1366
    assert t.htotal == 1784
    assert t.pixel_clock_khz == 85_250

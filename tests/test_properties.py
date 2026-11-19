"""Property-based tests: rules that must hold for any input hypothesis can find."""

import shutil
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_calc import _cvt_tool as cvt_tool
from test_calc import porches

from linuxcru.core.calc import cvt, cvt_rb2, scale_refresh
from linuxcru.core.edid import BLOCK_SIZE, Edid, EdidError, block_checksum_ok
from linuxcru.core.timing import (
    DTD_SIZE,
    MAX_PIXEL_CLOCK_KHZ,
    Timing,
    TimingError,
    decode_dtd,
    encode_dtd,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "auo_b156han02_96hz.bin").read_bytes()


@st.composite
def timings(draw: st.DrawFn) -> Timing:
    """Any timing a DTD can hold."""
    hfront = draw(st.integers(0, 0x3FF))
    hsync = draw(st.integers(1, 0x3FF))
    vfront = draw(st.integers(0, 0x3F))
    vsync = draw(st.integers(1, 0x3F))
    return Timing(
        pixel_clock_khz=draw(st.integers(1, MAX_PIXEL_CLOCK_KHZ // 10)) * 10,
        hactive=draw(st.integers(1, 0xFFF)),
        hfront=hfront,
        hsync=hsync,
        hback=draw(st.integers(0, 0xFFF - hfront - hsync)),
        vactive=draw(st.integers(1, 0xFFF)),
        vfront=vfront,
        vsync=vsync,
        vback=draw(st.integers(0, 0xFFF - vfront - vsync)),
        hsync_positive=draw(st.booleans()),
        vsync_positive=draw(st.booleans()),
        interlaced=draw(st.booleans()),
        width_mm=draw(st.integers(0, 0xFFF)),
        height_mm=draw(st.integers(0, 0xFFF)),
        hborder=draw(st.integers(0, 0xFF)),
        vborder=draw(st.integers(0, 0xFF)),
    )


@given(timings())
def test_any_valid_timing_survives_encode_and_decode(t: Timing) -> None:
    raw = encode_dtd(t)
    assert len(raw) == DTD_SIZE
    assert decode_dtd(raw) == t


@given(st.binary(min_size=DTD_SIZE, max_size=DTD_SIZE))
def test_decoding_random_bytes_never_crashes(raw: bytes) -> None:
    try:
        t = decode_dtd(raw)
    except TimingError:
        return
    assert encode_dtd(t) == raw if not t.problems() else True


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(timings(), min_size=1, max_size=7))
def test_rebuilt_edid_is_valid_and_keeps_other_data(modes: list[Timing]) -> None:
    original = Edid.from_bytes(FIXTURE)
    rebuilt = original.with_timings(modes)
    data = rebuilt.to_bytes()

    assert len(data) % BLOCK_SIZE == 0
    assert all(block_checksum_ok(b) for b in rebuilt.blocks)
    assert Edid.from_bytes(data).timings() == modes
    assert rebuilt.base[:54] == original.base[:54]  # header, vendor, colour data
    assert rebuilt.text_strings == original.text_strings


@given(st.floats(min_value=24, max_value=240, allow_nan=False))
def test_panel_method_hits_the_requested_refresh(refresh: float) -> None:
    native = Edid.from_bytes(FIXTURE).timings()[0]
    t = scale_refresh(native, refresh)
    # The clock is stored in 10 kHz steps, so the result can be off by at most
    # half a step: 5 kHz spread over every pixel of the frame.
    max_error = 5_000 / (t.htotal * t.vtotal)
    assert abs(t.refresh_hz - refresh) <= max_error + 1e-9
    assert (t.htotal, t.vtotal) == (native.htotal, native.vtotal)


@given(
    width=st.integers(640, 3840),
    height=st.integers(480, 2160),
    refresh=st.integers(24, 240),
    reduced=st.booleans(),
)
def test_cvt_timings_are_well_formed(width: int, height: int, refresh: int, reduced: bool) -> None:
    for t in (cvt(width, height, refresh, reduced=reduced), cvt_rb2(width, height, refresh)):
        assert t.resolution == (width, height)
        assert min(t.hfront, t.hsync, t.hback, t.vfront, t.vsync, t.vback) >= 0
        assert t.htotal > t.hactive and t.vtotal > t.vactive


@given(width=st.integers(640, 3840), height=st.integers(480, 2160), refresh=st.integers(24, 240))
def test_cvt_rb2_is_exact_up_to_clock_rounding(width: int, height: int, refresh: int) -> None:
    # RB2 computes the clock from the totals, rounded to 1 kHz and then to the
    # DTD's 10 kHz steps, so the only error is that rounding.
    t = cvt_rb2(width, height, refresh)
    assert abs(t.refresh_hz - refresh) <= 6_000 / (t.htotal * t.vtotal)


# Differential test: for random inputs, our CVT must equal the `cvt` tool's.
# (Guessing a tolerance doesn't work: at low refresh rates CVT's minimum
# blanking lines push the result well below the requested rate, by design.)
@pytest.mark.skipif(shutil.which("cvt") is None, reason="cvt tool not installed")
@given(
    width=st.integers(80, 480).map(lambda n: n * 8),
    height=st.integers(480, 2160),
    refresh=st.integers(24, 240),
    reduced=st.booleans(),
)
def test_cvt_matches_the_cvt_tool_for_random_modes(
    width: int, height: int, refresh: int, reduced: bool
) -> None:
    if reduced:
        refresh = max(60, refresh - refresh % 60)  # the tool only allows multiples of 60
    ours = cvt(width, height, refresh, reduced=reduced)
    assert porches(ours) == cvt_tool(width, height, refresh, reduced)


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(st.integers(8, 2 * BLOCK_SIZE - 1), st.integers(0, 255)), max_size=8))
def test_corrupted_edid_gives_clean_errors(changes: list[tuple[int, int]]) -> None:
    """Flip random bytes (keeping the header) and use the EDID like the app does."""
    data = bytearray(FIXTURE)
    for offset, value in changes:
        data[offset] = value
    try:
        edid = Edid.from_bytes(bytes(data), strict=False)
        edid.display_name  # noqa: B018 (property access is the test)
        for dt in edid.detailed_timings():
            dt.timing.label()
        edid.with_timings(edid.timings()[:1] or Edid.from_bytes(FIXTURE).timings()[:1])
    except (EdidError, TimingError):
        pass  # a clear, expected error is fine; anything else is a crash

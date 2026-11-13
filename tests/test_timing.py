import pytest

from linuxcru.core.edid import Edid
from linuxcru.core.timing import (
    MAX_PIXEL_CLOCK_KHZ,
    Timing,
    TimingError,
    decode_dtd,
    encode_dtd,
    round_pixel_clock_khz,
)

NATIVE_DTD = bytes.fromhex("7837 80b4 7038 2e40 6c30 aa00 58c1 1000 0018")
CTA_96HZ_DTD = bytes.fromhex("d05c 8018 7138 2d40 582c 4500 54be 1000 001e")


def test_decodes_native_60hz_mode() -> None:
    t = decode_dtd(NATIVE_DTD)
    assert t.pixel_clock_khz == 142_000
    assert t.resolution == (1920, 1080)
    assert (t.hfront, t.hsync, t.hback) == (108, 48, 24)
    assert (t.vfront, t.vsync, t.vback) == (10, 10, 26)
    assert (t.htotal, t.vtotal) == (2100, 1126)
    assert not t.hsync_positive and not t.vsync_positive
    assert not t.interlaced
    assert (t.width_mm, t.height_mm) == (344, 193)
    assert t.refresh_hz == pytest.approx(60.05244, abs=1e-5)
    assert t.hfreq_khz == pytest.approx(67.619, abs=1e-3)


def test_decodes_cea_style_96hz_mode() -> None:
    t = decode_dtd(CTA_96HZ_DTD)
    assert t.pixel_clock_khz == 237_600
    assert (t.hfront, t.hsync, t.hback) == (88, 44, 148)
    assert (t.vfront, t.vsync, t.vback) == (4, 5, 36)
    assert t.hsync_positive and t.vsync_positive
    assert t.refresh_hz == pytest.approx(96.0)
    assert t.label() == "1920x1080 @ 96.000 Hz"


@pytest.mark.parametrize("raw", [NATIVE_DTD, CTA_96HZ_DTD])
def test_round_trip_is_byte_exact(raw: bytes) -> None:
    assert encode_dtd(decode_dtd(raw)) == raw


def test_round_trip_every_dtd_in_fixture(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    for dt in edid.detailed_timings():
        raw = edid.blocks[dt.block][dt.offset : dt.offset + 18]
        assert encode_dtd(dt.timing) == raw


def test_round_trip_large_values_use_high_bits() -> None:
    t = Timing(
        pixel_clock_khz=594_000,
        hactive=3840,
        hfront=176,
        hsync=88,
        hback=296,
        vactive=2160,
        vfront=8,
        vsync=10,
        vback=72,
        width_mm=1600,
        height_mm=900,
    )
    assert decode_dtd(encode_dtd(t)) == t


def test_large_porches_round_trip() -> None:
    t = Timing(300_000, 1920, 1000, 1000, 50, 1080, 60, 60, 5)
    assert decode_dtd(encode_dtd(t)) == t


def test_composite_sync_keeps_bit_2() -> None:
    raw = bytearray(NATIVE_DTD)
    raw[17] = 0x14  # digital composite, serrations on
    assert encode_dtd(decode_dtd(bytes(raw))) == bytes(raw)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pixel_clock_khz": MAX_PIXEL_CLOCK_KHZ + 10}, "pixel clock"),
        ({"pixel_clock_khz": 142_005}, "multiple of 10"),
        ({"hfront": 1024}, "hfront"),
        ({"vfront": 64}, "vfront"),
        ({"vsync": 0}, "sync width"),
        ({"hback": -1}, "negative"),
        ({"hactive": 5000}, "hactive"),
    ],
)
def test_rejects_values_a_dtd_cannot_hold(changes: dict[str, int], message: str) -> None:
    base = decode_dtd(NATIVE_DTD)
    bad = Timing(**{**base.__dict__, **changes})
    with pytest.raises(TimingError, match=message):
        encode_dtd(bad)


def test_rejects_non_timing_descriptor() -> None:
    with pytest.raises(TimingError, match="zero pixel clock"):
        decode_dtd(bytes(18))


def test_rounds_clock_to_10khz() -> None:
    assert round_pixel_clock_khz(227_001.6) == 227_000
    assert round_pixel_clock_khz(227_005.1) == 227_010


def test_edid_lists_timings_in_order(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    found = edid.detailed_timings()
    assert [(d.block, d.offset) for d in found] == [(0, 54), (1, 4)]
    assert [round(d.timing.refresh_hz, 2) for d in found] == [60.05, 96.0]
    assert edid.preferred_timing == found[0].timing
    assert found[1].location == "extension block 1"

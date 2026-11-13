import shutil
from pathlib import Path

import pytest
from conftest import edid_decode_failures

from linuxcru.core.calc import Method, calculate, scale_refresh
from linuxcru.core.edid import (
    BLOCK_SIZE,
    EXT_CTA,
    TAG_RANGE_LIMITS,
    Edid,
    EdidError,
    with_fixed_checksum,
)

needs_edid_decode = pytest.mark.skipif(
    shutil.which("edid-decode") is None, reason="edid-decode not installed"
)


def test_rebuilding_with_same_timings_is_byte_exact(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    assert edid.with_timings(edid.timings()).to_bytes() == laptop_edid_bytes


def test_adding_to_base_only_edid_creates_cta_block(
    base_only_bytes: bytes, laptop_edid_bytes: bytes
) -> None:
    # Adding the same 96 Hz mode drmcru added must give exactly its file.
    mode_96 = Edid.from_bytes(laptop_edid_bytes).timings()[1]
    result = Edid.from_bytes(base_only_bytes).with_timing_added(mode_96)
    assert result.to_bytes() == laptop_edid_bytes
    assert result.extension_tags == [EXT_CTA]


def test_added_mode_is_appended(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    native = edid.timings()[0]
    result = edid.with_timing_added(scale_refresh(native, 120))
    assert [round(t.refresh_hz) for t in result.timings()] == [60, 96, 120]
    assert result.checksums_ok()
    assert Edid.from_bytes(result.to_bytes()).timings() == result.timings()


def test_similar_mode_is_replaced_not_duplicated(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    panel_96 = calculate(Method.PANEL, 96, base=edid.timings()[0])
    result = edid.with_timing_added(panel_96)
    assert result.timings() == [edid.timings()[0], panel_96]


def test_preferred_mode_goes_first(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    native = edid.timings()[0]
    fast = scale_refresh(native, 90)
    result = edid.with_timing_added(fast, preferred=True)
    assert result.preferred_timing == fast
    assert result.timings()[1] == native
    first = result.detailed_timings()[0]
    assert (first.block, first.offset) == (0, 54)


def test_other_descriptors_are_kept(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    result = edid.with_timing_added(scale_refresh(edid.timings()[0], 75), preferred=True)
    assert result.base[72:126] == edid.base[72:126]
    assert result.text_strings == ["AUO", "B156HAN02.1"]
    assert result.base[:54] == edid.base[:54]


def test_removing_all_but_one_timing(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    result = edid.with_timings(edid.timings()[:1])
    assert result.timings() == edid.timings()[:1]
    assert result.extension_count == 1


def test_capacity_limits(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    native = edid.timings()[0]
    assert edid.timing_capacity() == 1 + 6
    many = [scale_refresh(native, 50 + i) for i in range(7)]
    assert len(edid.with_timings(many).timings()) == 7
    with pytest.raises(EdidError, match="too many"):
        edid.with_timings([*many, scale_refresh(native, 99)])
    with pytest.raises(EdidError, match="at least one"):
        edid.with_timings([])


def test_cta_data_blocks_survive(laptop_edid_bytes: bytes) -> None:
    data = bytearray(laptop_edid_bytes)
    cta = bytearray(data[BLOCK_SIZE:])
    # Insert a 3-byte video data block (VIC 16 = 1080p60) before the DTDs.
    dtd = bytes(cta[4:22])
    cta[2] = 7
    cta[4:7] = bytes([0x42, 0x90, 0x10])
    cta[7:25] = dtd
    cta[25:127] = bytes(102)
    data[BLOCK_SIZE:] = with_fixed_checksum(bytes(cta))
    edid = Edid.from_bytes(bytes(data))
    assert len(edid.timings()) == 2

    result = edid.with_timing_added(scale_refresh(edid.timings()[0], 120))
    block = result.blocks[1]
    assert block[2] == 7
    assert block[4:7] == bytes([0x42, 0x90, 0x10])
    assert len(result.timings()) == 3


def test_range_limits_are_widened(base_only_bytes: bytes) -> None:
    base = bytearray(base_only_bytes)
    # Slot 2 (offset 90): range limits 40-60 Hz, 30-70 kHz, 150 MHz max.
    base[90:108] = bytes([0, 0, 0, TAG_RANGE_LIMITS, 0, 40, 60, 30, 70, 15, 0, 0x0A]) + bytes(
        [0x20] * 6
    )
    edid = Edid.from_bytes(with_fixed_checksum(bytes(base)))
    result = edid.with_timing_added(scale_refresh(edid.timings()[0], 96))
    limits = result.base[90:108]
    assert (limits[5], limits[6]) == (40, 96)
    assert (limits[7], limits[8]) == (30, 109)
    assert limits[9] == 23


@needs_edid_decode
@pytest.mark.parametrize("method", list(Method))
def test_edid_decode_accepts_our_output(
    method: Method, laptop_edid_bytes: bytes, base_only_bytes: bytes, tmp_path: Path
) -> None:
    baseline = edid_decode_failures(laptop_edid_bytes, tmp_path)
    for source in (laptop_edid_bytes, base_only_bytes):
        edid = Edid.from_bytes(source)
        mode = calculate(method, 96, base=edid.timings()[0])
        for preferred in (False, True):
            out = edid.with_timing_added(mode, preferred=preferred).to_bytes()
            assert edid_decode_failures(out, tmp_path) <= baseline

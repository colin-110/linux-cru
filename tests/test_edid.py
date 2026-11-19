import pytest

from linuxcru.core.edid import (
    BLOCK_SIZE,
    EXT_CTA,
    Edid,
    EdidError,
    block_checksum_ok,
    checksum_byte,
    decode_manufacturer_id,
    with_fixed_checksum,
)


def test_loads_both_blocks(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    assert edid.extension_count == 1
    assert edid.extension_tags == [EXT_CTA]
    assert edid.to_bytes() == laptop_edid_bytes


def test_identifies_the_panel(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    assert edid.manufacturer_id == "AUO"
    assert edid.product_code == 0xDF87
    assert edid.manufacture_year == 2019
    assert edid.version == (1, 4)
    assert edid.is_digital
    assert edid.screen_size_cm == (34, 19)
    assert edid.product_name is None
    assert edid.text_strings == ["AUO", "B156HAN02.1"]
    assert edid.display_name == "AUO B156HAN02.1"


def test_descriptor_slots(laptop_edid_bytes: bytes) -> None:
    descs = Edid.from_bytes(laptop_edid_bytes).descriptors()
    assert [d.is_timing for d in descs] == [True, False, False, False]
    assert [d.tag for d in descs] == [None, 0x0F, 0xFE, 0xFE]


def test_checksum_helpers(laptop_edid_bytes: bytes) -> None:
    base = laptop_edid_bytes[:BLOCK_SIZE]
    assert block_checksum_ok(base)
    assert checksum_byte(base) == base[127]

    broken = bytearray(base)
    broken[40] ^= 0xFF
    assert not block_checksum_ok(bytes(broken))
    assert block_checksum_ok(with_fixed_checksum(bytes(broken)))


def test_rejects_bad_checksum(laptop_edid_bytes: bytes) -> None:
    data = bytearray(laptop_edid_bytes)
    data[200] ^= 0x01
    with pytest.raises(EdidError, match="block 1"):
        Edid.from_bytes(bytes(data))
    assert not Edid.from_bytes(bytes(data), strict=False).checksums_ok()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "too short"),
        (bytes(130), "multiple of 128"),
        (bytes(128), "header"),
    ],
)
def test_rejects_malformed_input(data: bytes, message: str) -> None:
    with pytest.raises(EdidError, match=message):
        Edid.from_bytes(data)


def test_rejects_missing_extension(base_only_bytes: bytes) -> None:
    data = bytearray(base_only_bytes)
    data[126] = 2
    with pytest.raises(EdidError, match="declares 2"):
        Edid.from_bytes(with_fixed_checksum(bytes(data)))


def test_ignores_trailing_blocks_not_declared(base_only_bytes: bytes) -> None:
    edid = Edid.from_bytes(base_only_bytes + bytes(BLOCK_SIZE))
    assert edid.extension_count == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(b"\x06\xaf", "AUO"), (b"\x4c\x2d", "SAM"), (b"\x00\x00", "???")],
)
def test_manufacturer_ids(raw: bytes, expected: str) -> None:
    assert decode_manufacturer_id(raw) == expected


# --------------------------------------------------------- error paths


def test_checksum_byte_needs_a_full_block() -> None:
    with pytest.raises(EdidError, match="128 bytes"):
        checksum_byte(bytes(127))


def test_edid_needs_a_block() -> None:
    with pytest.raises(EdidError, match="at least one block"):
        Edid([])


def test_serial_and_non_text_descriptors(laptop_edid_bytes: bytes) -> None:
    edid = Edid.from_bytes(laptop_edid_bytes)
    assert edid.serial_number == 0
    descs = edid.descriptors()
    assert descs[0].text is None  # a timing
    assert descs[1].text is None  # manufacturer data, not text


def test_product_name_descriptor_is_preferred(base_only_bytes: bytes) -> None:
    base = bytearray(base_only_bytes)
    base[108:126] = bytes([0, 0, 0, 0xFC, 0]) + b"Test Panel\n  "
    edid = Edid.from_bytes(with_fixed_checksum(bytes(base)))
    assert edid.product_name == "Test Panel"
    assert edid.display_name == "AUO Test Panel"


def test_cta_offsets_ignore_other_extension_types() -> None:
    from linuxcru.core.edid import EXT_DISPLAYID, cta_dtd_offsets

    assert cta_dtd_offsets(bytes([EXT_DISPLAYID, 0x03, 0x04]) + bytes(125)) == []


def test_fill_cta_block_refuses_overflow() -> None:
    from linuxcru.core.edid import _fill_cta_block, new_cta_block

    dtd = bytes([0x78, 0x37]) + bytes(16)
    with pytest.raises(EdidError, match="too many DTDs"):
        _fill_cta_block(new_cta_block(), [dtd] * 7)


def test_no_room_for_a_new_extension_block(base_only_bytes: bytes) -> None:
    from linuxcru.core.edid import MAX_BLOCKS

    filler = with_fixed_checksum(bytes([0x70]) + bytes(127))  # DisplayID, no CTA
    edid = Edid([base_only_bytes] + [filler] * (MAX_BLOCKS - 1))
    timings = Edid.from_bytes(base_only_bytes).timings() * 2
    with pytest.raises(EdidError, match="no room"):
        edid.with_timings(timings)

"""Reading, validating and rebuilding EDID data.

An EDID is a list of 128-byte blocks. Block 0 is the base block; any further
blocks are extensions (CTA-861, DisplayID, ...). Every block ends with a
checksum byte chosen so that the 128 bytes add up to a multiple of 256.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from linuxcru.core.timing import DTD_SIZE, Timing, decode_dtd, encode_dtd, is_timing_descriptor

BLOCK_SIZE = 128
HEADER = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
EXTENSION_COUNT_OFFSET = 126
MAX_BLOCKS = 256

# Base block: four 18-byte descriptor slots.
DESCRIPTOR_OFFSETS = (54, 72, 90, 108)
DESCRIPTOR_SIZE = 18

# Display descriptor tags (byte 3 of a descriptor whose pixel clock is 0).
TAG_SERIAL = 0xFF
TAG_ALPHANUMERIC = 0xFE
TAG_RANGE_LIMITS = 0xFD
TAG_PRODUCT_NAME = 0xFC
TAG_DUMMY = 0x10

# Extension block tags.
EXT_CTA = 0x02
EXT_DISPLAYID = 0x70

EXTENSION_NAMES = {
    EXT_CTA: "CTA-861",
    0x10: "Video Timing Block",
    0x40: "DI-EXT",
    0x50: "LS-EXT",
    0x60: "DPVL",
    EXT_DISPLAYID: "DisplayID",
    0xF0: "Block Map",
}


class EdidError(ValueError):
    """Raised when EDID data is malformed or an edit is impossible."""


def checksum_byte(block: bytes) -> int:
    """Return the value byte 127 must have for ``block`` to be valid."""
    if len(block) != BLOCK_SIZE:
        raise EdidError(f"block must be {BLOCK_SIZE} bytes, got {len(block)}")
    return (-sum(block[:127])) % 256


def block_checksum_ok(block: bytes) -> bool:
    return len(block) == BLOCK_SIZE and sum(block) % 256 == 0


def with_fixed_checksum(block: bytes) -> bytes:
    fixed = bytearray(block)
    fixed[127] = checksum_byte(bytes(block))
    return bytes(fixed)


def decode_manufacturer_id(raw: bytes) -> str:
    """Decode the 3-letter PNP vendor id packed into two big-endian bytes."""
    value = (raw[0] << 8) | raw[1]
    letters = [(value >> shift) & 0x1F for shift in (10, 5, 0)]
    if not all(1 <= c <= 26 for c in letters):
        return "???"
    return "".join(chr(ord("A") + c - 1) for c in letters)


def _descriptor_text(desc: bytes) -> str:
    text = desc[5:18].split(b"\n", 1)[0]
    return text.decode("cp437", errors="replace").strip()


@dataclass(frozen=True)
class Descriptor:
    """One of the four 18-byte descriptor slots of the base block."""

    slot: int
    data: bytes

    @property
    def is_timing(self) -> bool:
        return self.data[0] != 0 or self.data[1] != 0

    @property
    def tag(self) -> int | None:
        return None if self.is_timing else self.data[3]

    @property
    def text(self) -> str | None:
        if self.tag in (TAG_SERIAL, TAG_ALPHANUMERIC, TAG_PRODUCT_NAME):
            return _descriptor_text(self.data)
        return None


@dataclass(frozen=True)
class DetailedTiming:
    """A timing found in the EDID, plus where it is stored."""

    timing: Timing
    block: int
    offset: int

    @property
    def location(self) -> str:
        return "base block" if self.block == 0 else f"extension block {self.block}"


def cta_dtd_offsets(block: bytes) -> list[int]:
    """Offsets of the DTDs stored in a CTA-861 extension block.

    Byte 2 says where the DTDs start (0 means there are none). They run until
    a descriptor with a zero pixel clock or until the checksum byte.
    """
    start = block[2]
    if block[0] != EXT_CTA or start < 4:
        return []
    offsets = []
    offset = start
    while offset + DTD_SIZE <= 127 and is_timing_descriptor(block[offset : offset + DTD_SIZE]):
        offsets.append(offset)
        offset += DTD_SIZE
    return offsets


DUMMY_DESCRIPTOR = bytes([0, 0, 0, TAG_DUMMY]) + bytes(DESCRIPTOR_SIZE - 4)
SIMILAR_REFRESH_HZ = 0.5


def new_cta_block() -> bytes:
    """An empty CTA-861 rev 3 block: no data blocks, DTDs start at byte 4."""
    block = bytearray(BLOCK_SIZE)
    block[0:4] = bytes([EXT_CTA, 0x03, 0x04, 0x00])
    return with_fixed_checksum(bytes(block))


def cta_capacity(block: bytes) -> int:
    """How many DTDs fit after the data block collection of a CTA block."""
    start = block[2] if block[2] >= 4 else 4
    return (127 - start) // DTD_SIZE


def _fill_cta_block(block: bytes, dtds: Sequence[bytes]) -> bytes:
    """Replace the DTDs of a CTA block, keeping its data blocks."""
    start = block[2] if block[2] >= 4 else 4
    if len(dtds) > cta_capacity(block):
        raise EdidError("too many DTDs for this CTA block")
    new = bytearray(block[:start]) + b"".join(dtds)
    new += bytes(BLOCK_SIZE - len(new))
    new[2] = start
    native = min(block[3] & 0x0F, len(dtds))
    new[3] = (block[3] & 0xF0) | native
    return with_fixed_checksum(bytes(new))


def _relax_range_limits(base: bytearray, timings: Sequence[Timing]) -> None:
    """Widen a Display Range Limits descriptor so it covers ``timings``.

    Only plain (no offset flags) limits are adjusted; values are only ever
    widened, never narrowed.
    """
    for offset in DESCRIPTOR_OFFSETS:
        desc = base[offset : offset + DESCRIPTOR_SIZE]
        if is_timing_descriptor(bytes(desc)) or desc[3] != TAG_RANGE_LIMITS or desc[4]:
            continue
        min_v = math.floor(min(t.refresh_hz for t in timings))
        max_v = math.ceil(max(t.refresh_hz for t in timings))
        min_h = math.floor(min(t.hfreq_khz for t in timings))
        max_h = math.ceil(max(t.hfreq_khz for t in timings))
        max_clock = math.ceil(max(t.pixel_clock_khz for t in timings) / 10_000)
        base[offset + 5] = max(1, min(base[offset + 5], min_v))
        base[offset + 6] = min(255, max(base[offset + 6], max_v))
        base[offset + 7] = max(1, min(base[offset + 7], min_h))
        base[offset + 8] = min(255, max(base[offset + 8], max_h))
        base[offset + 9] = min(255, max(base[offset + 9], max_clock))


class Edid:
    """An EDID made of a base block and zero or more extension blocks."""

    def __init__(self, blocks: list[bytes]) -> None:
        if not blocks:
            raise EdidError("an EDID needs at least one block")
        self._blocks = [bytes(b) for b in blocks]

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_bytes(cls, data: bytes, *, strict: bool = True) -> Edid:
        """Parse raw EDID bytes.

        With ``strict`` (the default) bad checksums raise an error. Without it
        they are accepted, which is useful for inspecting broken data.
        """
        if len(data) < BLOCK_SIZE:
            raise EdidError(f"EDID too short: {len(data)} bytes")
        if len(data) % BLOCK_SIZE:
            raise EdidError(f"EDID length {len(data)} is not a multiple of {BLOCK_SIZE}")
        if data[:8] != HEADER:
            raise EdidError("missing EDID header (00 FF FF FF FF FF FF 00)")

        declared = data[EXTENSION_COUNT_OFFSET]
        available = len(data) // BLOCK_SIZE - 1
        if declared > available:
            raise EdidError(
                f"base block declares {declared} extension(s) but only {available} present"
            )
        blocks = [data[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE] for i in range(declared + 1)]

        if strict:
            for index, block in enumerate(blocks):
                if not block_checksum_ok(block):
                    raise EdidError(f"bad checksum in block {index}")
        return cls(blocks)

    @classmethod
    def from_file(cls, path: str | Path, *, strict: bool = True) -> Edid:
        return cls.from_bytes(Path(path).read_bytes(), strict=strict)

    def to_bytes(self) -> bytes:
        return b"".join(self._blocks)

    # ------------------------------------------------------------- properties

    @property
    def blocks(self) -> list[bytes]:
        return list(self._blocks)

    @property
    def base(self) -> bytes:
        return self._blocks[0]

    @property
    def extension_count(self) -> int:
        return len(self._blocks) - 1

    @property
    def extension_tags(self) -> list[int]:
        return [block[0] for block in self._blocks[1:]]

    @property
    def manufacturer_id(self) -> str:
        return decode_manufacturer_id(self.base[8:10])

    @property
    def product_code(self) -> int:
        return int.from_bytes(self.base[10:12], "little")

    @property
    def serial_number(self) -> int:
        return int.from_bytes(self.base[12:16], "little")

    @property
    def manufacture_year(self) -> int | None:
        year = self.base[17]
        return 1990 + year if year else None

    @property
    def version(self) -> tuple[int, int]:
        return self.base[18], self.base[19]

    @property
    def is_digital(self) -> bool:
        return bool(self.base[20] & 0x80)

    @property
    def screen_size_cm(self) -> tuple[int, int]:
        return self.base[21], self.base[22]

    def descriptors(self) -> list[Descriptor]:
        return [
            Descriptor(slot, self.base[offset : offset + DESCRIPTOR_SIZE])
            for slot, offset in enumerate(DESCRIPTOR_OFFSETS)
        ]

    @property
    def product_name(self) -> str | None:
        for desc in self.descriptors():
            if desc.tag == TAG_PRODUCT_NAME:
                return desc.text
        return None

    @property
    def text_strings(self) -> list[str]:
        """Alphanumeric data strings (laptop panels often put the model here)."""
        return [d.text for d in self.descriptors() if d.tag == TAG_ALPHANUMERIC and d.text]

    @property
    def display_name(self) -> str:
        """Best human-readable name for the display."""
        if self.product_name:
            return f"{self.manufacturer_id} {self.product_name}"
        strings = self.text_strings
        model = strings[-1] if strings else f"{self.product_code:04X}"
        return f"{self.manufacturer_id} {model}"

    def checksums_ok(self) -> bool:
        return all(block_checksum_ok(b) for b in self._blocks)

    def detailed_timings(self) -> list[DetailedTiming]:
        """All DTDs, in order. The first one is the preferred (native) mode."""
        found = [
            DetailedTiming(decode_dtd(d.data), 0, DESCRIPTOR_OFFSETS[d.slot])
            for d in self.descriptors()
            if d.is_timing
        ]
        for index, block in enumerate(self._blocks[1:], start=1):
            for offset in cta_dtd_offsets(block):
                found.append(
                    DetailedTiming(decode_dtd(block[offset : offset + DTD_SIZE]), index, offset)
                )
        return found

    def timings(self) -> list[Timing]:
        return [dt.timing for dt in self.detailed_timings()]

    @property
    def preferred_timing(self) -> Timing | None:
        timings = self.timings()
        return timings[0] if timings else None

    # ---------------------------------------------------------------- editing

    def timing_capacity(self) -> int:
        """How many detailed timings this EDID can hold after a rebuild."""
        base_slots = sum(1 for d in self.descriptors() if d.is_timing or d.tag == TAG_DUMMY)
        cta = [b for b in self._blocks[1:] if b[0] == EXT_CTA]
        if cta:
            return base_slots + sum(cta_capacity(b) for b in cta)
        return base_slots + cta_capacity(new_cta_block())

    def with_timings(self, timings: Sequence[Timing]) -> Edid:
        """Return a copy whose detailed timings are exactly ``timings``.

        The first timing becomes the preferred mode. Timings go into the base
        block slots that already hold timings (or are unused), then into the
        CTA-861 extension, which is created if needed. Every other byte, such
        as the panel's name and serial descriptors, is kept.
        """
        if not timings:
            raise EdidError("at least one detailed timing is required")
        if len(timings) > self.timing_capacity():
            raise EdidError(
                f"too many detailed timings ({len(timings)}); "
                f"this EDID has room for {self.timing_capacity()}"
            )
        queue = [encode_dtd(t) for t in timings]

        base = bytearray(self.base)
        for desc in self.descriptors():
            if desc.is_timing or desc.tag == TAG_DUMMY:
                offset = DESCRIPTOR_OFFSETS[desc.slot]
                base[offset : offset + DESCRIPTOR_SIZE] = (
                    queue.pop(0) if queue else DUMMY_DESCRIPTOR
                )

        blocks = [bytes(base), *self._blocks[1:]]
        cta_indexes = [i for i, b in enumerate(blocks) if i > 0 and b[0] == EXT_CTA]
        if queue and not cta_indexes:
            if len(blocks) >= MAX_BLOCKS:
                raise EdidError("no room for another extension block")
            blocks.append(new_cta_block())
            cta_indexes.append(len(blocks) - 1)
        for index in cta_indexes:
            take = queue[: cta_capacity(blocks[index])]
            del queue[: len(take)]
            blocks[index] = _fill_cta_block(blocks[index], take)

        base = bytearray(blocks[0])
        base[EXTENSION_COUNT_OFFSET] = len(blocks) - 1
        _relax_range_limits(base, timings)
        blocks[0] = with_fixed_checksum(bytes(base))
        return Edid(blocks)

    def with_timing_added(self, timing: Timing, *, preferred: bool = False) -> Edid:
        """Add a mode, replacing any existing one that is effectively the same.

        "The same" means equal resolution and a refresh rate within 0.5 Hz, so
        adding 96 Hz twice does not leave two 96 Hz modes behind.
        """
        timings = [
            t
            for t in self.timings()
            if not (
                t.resolution == timing.resolution
                and abs(t.refresh_hz - timing.refresh_hz) < SIMILAR_REFRESH_HZ
            )
        ]
        if preferred:
            timings.insert(0, timing)
        else:
            timings.append(timing)
        return self.with_timings(timings)

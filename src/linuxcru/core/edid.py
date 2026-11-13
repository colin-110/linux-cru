"""Reading, validating and rebuilding EDID data.

An EDID is a list of 128-byte blocks. Block 0 is the base block; any further
blocks are extensions (CTA-861, DisplayID, ...). Every block ends with a
checksum byte chosen so that the 128 bytes add up to a multiple of 256.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

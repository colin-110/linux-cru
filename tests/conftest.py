from pathlib import Path

import pytest

from linuxcru.core.edid import BLOCK_SIZE, with_fixed_checksum

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def laptop_edid_bytes() -> bytes:
    """AUO B156HAN02.1 panel with an extra 96 Hz mode in a CTA-861 block."""
    return (FIXTURES / "auo_b156han02_96hz.bin").read_bytes()


@pytest.fixture
def base_only_bytes(laptop_edid_bytes: bytes) -> bytes:
    """The same panel's base block on its own, with no extension blocks."""
    base = bytearray(laptop_edid_bytes[:BLOCK_SIZE])
    base[126] = 0
    return with_fixed_checksum(bytes(base))

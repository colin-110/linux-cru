import re
import subprocess
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


def edid_decode_failures(data: bytes, tmp_path: Path) -> set[str]:
    """Problems edid-decode reports, ignoring which block number they are in."""
    path = tmp_path / "check.bin"
    path.write_bytes(data)
    out = subprocess.run(
        ["edid-decode", "--check", str(path)], capture_output=True, text=True
    ).stdout
    if "Invalid checksum" in out or re.search(r"Checksum: 0x[0-9a-f]+ \(should be", out):
        raise AssertionError(f"edid-decode reports a bad checksum:\n{out}")
    failures = out.split("Failures:", 1)[1] if "Failures:" in out else ""
    failures = failures.split("EDID conformity", 1)[0]
    return {
        line.strip()
        for line in failures.splitlines()
        if line.startswith("  ") and not line.strip().startswith("Block")
    }

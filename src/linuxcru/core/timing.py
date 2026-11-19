"""Display timings and the 18-byte Detailed Timing Descriptor (DTD) format.

A frame is drawn line by line. Each line has the visible pixels (active),
then a blanking interval made of the front porch, the sync pulse and the back
porch. Lines work the same way vertically. The pixel clock is how many pixels
are sent per second, so::

    refresh = pixel_clock / (h_total * v_total)
"""

from __future__ import annotations

from dataclasses import dataclass, replace

DTD_SIZE = 18

# Bits 4-3 of byte 17 = 0b11: digital separate sync. Polarity lives in bits 2-1.
SYNC_DIGITAL_SEPARATE = 0x18
_SYNC_TYPE_MASK = 0x18
_HSYNC_POSITIVE = 0x02
_VSYNC_POSITIVE = 0x04
_INTERLACED = 0x80

# Largest values each DTD field can hold (from the field bit widths).
MAX_PIXEL_CLOCK_KHZ = 0xFFFF * 10  # 655.35 MHz, stored in 10 kHz units
_LIMITS = {
    "hactive": 0xFFF,
    "hblank": 0xFFF,
    "vactive": 0xFFF,
    "vblank": 0xFFF,
    "hfront": 0x3FF,
    "hsync": 0x3FF,
    "vfront": 0x3F,
    "vsync": 0x3F,
    "width_mm": 0xFFF,
    "height_mm": 0xFFF,
    "hborder": 0xFF,
    "vborder": 0xFF,
}


class TimingError(ValueError):
    """Raised for timings that are invalid or cannot be stored in a DTD."""


@dataclass(frozen=True)
class Timing:
    """One display mode, in the same units a DTD uses."""

    pixel_clock_khz: int
    hactive: int
    hfront: int
    hsync: int
    hback: int
    vactive: int
    vfront: int
    vsync: int
    vback: int
    hsync_positive: bool = True
    vsync_positive: bool = False
    interlaced: bool = False
    width_mm: int = 0
    height_mm: int = 0
    hborder: int = 0
    vborder: int = 0
    # Byte 17 bits other than interlace and polarity (sync type, stereo).
    # Kept so that decode -> encode gives back the exact original bytes.
    flags: int = SYNC_DIGITAL_SEPARATE

    # ----------------------------------------------------------- derived values

    @property
    def hblank(self) -> int:
        return self.hfront + self.hsync + self.hback

    @property
    def vblank(self) -> int:
        return self.vfront + self.vsync + self.vback

    @property
    def htotal(self) -> int:
        return self.hactive + self.hblank

    @property
    def vtotal(self) -> int:
        return self.vactive + self.vblank

    @property
    def pixel_clock_mhz(self) -> float:
        return self.pixel_clock_khz / 1000

    @property
    def hfreq_khz(self) -> float:
        return self.pixel_clock_khz / self.htotal if self.htotal else 0.0

    @property
    def refresh_hz(self) -> float:
        """Vertical refresh rate (field rate for interlaced modes); 0 if undefined."""
        frame = self.htotal * self.vtotal
        return self.pixel_clock_khz * 1000 / frame if frame else 0.0

    @property
    def resolution(self) -> tuple[int, int]:
        return self.hactive, self.vactive

    def label(self) -> str:
        scan = "i" if self.interlaced else ""
        return f"{self.hactive}x{self.vactive}{scan} @ {self.refresh_hz:.3f} Hz"

    def with_pixel_clock(self, pixel_clock_khz: int) -> Timing:
        return replace(self, pixel_clock_khz=pixel_clock_khz)

    # -------------------------------------------------------------- validation

    def problems(self) -> list[str]:
        """Everything that prevents this timing from being stored in a DTD."""
        issues: list[str] = []
        if not 10 <= self.pixel_clock_khz <= MAX_PIXEL_CLOCK_KHZ:
            issues.append(
                f"pixel clock {self.pixel_clock_mhz:.2f} MHz is outside "
                f"0.01-{MAX_PIXEL_CLOCK_KHZ / 1000:.2f} MHz"
            )
        if self.pixel_clock_khz % 10:
            issues.append("pixel clock must be a multiple of 10 kHz")
        if self.hactive <= 0 or self.vactive <= 0:
            issues.append("active size must be positive")
        for name in ("hfront", "hsync", "hback", "vfront", "vsync", "vback"):
            if getattr(self, name) < 0:
                issues.append(f"{name} cannot be negative")
        if self.hsync == 0 or self.vsync == 0:
            issues.append("sync width cannot be zero")
        for name, limit in _LIMITS.items():
            value = getattr(self, name)
            if value > limit:
                issues.append(f"{name} = {value} exceeds the DTD limit of {limit}")
        return issues

    def validate(self) -> None:
        issues = self.problems()
        if issues:
            raise TimingError("; ".join(issues))


def _extra_flags(features: int) -> int:
    """Byte 17 without the bits that Timing stores as separate fields.

    Bit 2 is the vsync polarity only for digital separate sync; for other sync
    types it means something else, so it is kept as-is in that case.
    """
    mask = _INTERLACED | _HSYNC_POSITIVE
    if features & _SYNC_TYPE_MASK == SYNC_DIGITAL_SEPARATE:
        mask |= _VSYNC_POSITIVE
    return features & ~mask & 0xFF


def round_pixel_clock_khz(khz: float) -> int:
    """Round a pixel clock to the 10 kHz steps a DTD can store."""
    return int(round(khz / 10)) * 10


def is_timing_descriptor(data: bytes) -> bool:
    """A descriptor holds a timing when its pixel clock field is non-zero."""
    return len(data) >= 2 and (data[0] or data[1]) != 0


def decode_dtd(data: bytes) -> Timing:
    """Decode an 18-byte Detailed Timing Descriptor."""
    if len(data) != DTD_SIZE:
        raise TimingError(f"a DTD is {DTD_SIZE} bytes, got {len(data)}")
    if not is_timing_descriptor(data):
        raise TimingError("descriptor has a zero pixel clock (not a timing)")

    hactive = data[2] | (data[4] & 0xF0) << 4
    hblank = data[3] | (data[4] & 0x0F) << 8
    vactive = data[5] | (data[7] & 0xF0) << 4
    vblank = data[6] | (data[7] & 0x0F) << 8

    hfront = data[8] | (data[11] & 0xC0) << 2
    hsync = data[9] | (data[11] & 0x30) << 4
    vfront = (data[10] >> 4) | (data[11] & 0x0C) << 2
    vsync = (data[10] & 0x0F) | (data[11] & 0x03) << 4

    if hactive == 0 or vactive == 0:
        raise TimingError("descriptor has a zero active size")
    hback = hblank - hfront - hsync
    vback = vblank - vfront - vsync
    if hback < 0 or vback < 0:
        raise TimingError("porches and sync are larger than the blanking interval")

    features = data[17]
    digital_separate = features & _SYNC_TYPE_MASK == SYNC_DIGITAL_SEPARATE
    return Timing(
        pixel_clock_khz=int.from_bytes(data[0:2], "little") * 10,
        hactive=hactive,
        hfront=hfront,
        hsync=hsync,
        hback=hback,
        vactive=vactive,
        vfront=vfront,
        vsync=vsync,
        vback=vback,
        hsync_positive=bool(features & _HSYNC_POSITIVE),
        vsync_positive=digital_separate and bool(features & _VSYNC_POSITIVE),
        interlaced=bool(features & _INTERLACED),
        width_mm=data[12] | (data[14] & 0xF0) << 4,
        height_mm=data[13] | (data[14] & 0x0F) << 8,
        hborder=data[15],
        vborder=data[16],
        flags=_extra_flags(features),
    )


def encode_dtd(timing: Timing) -> bytes:
    """Encode a timing as an 18-byte Detailed Timing Descriptor."""
    timing.validate()
    t = timing
    clock = t.pixel_clock_khz // 10

    features = _extra_flags(t.flags)
    if t.interlaced:
        features |= _INTERLACED
    if t.hsync_positive:
        features |= _HSYNC_POSITIVE
    if t.vsync_positive and features & _SYNC_TYPE_MASK == SYNC_DIGITAL_SEPARATE:
        features |= _VSYNC_POSITIVE

    return bytes(
        [
            clock & 0xFF,
            clock >> 8,
            t.hactive & 0xFF,
            t.hblank & 0xFF,
            (t.hactive >> 8) << 4 | t.hblank >> 8,
            t.vactive & 0xFF,
            t.vblank & 0xFF,
            (t.vactive >> 8) << 4 | t.vblank >> 8,
            t.hfront & 0xFF,
            t.hsync & 0xFF,
            (t.vfront & 0x0F) << 4 | t.vsync & 0x0F,
            (t.hfront >> 8) << 6 | (t.hsync >> 8) << 4 | (t.vfront >> 4) << 2 | t.vsync >> 4,
            t.width_mm & 0xFF,
            t.height_mm & 0xFF,
            (t.width_mm >> 8) << 4 | t.height_mm >> 8,
            t.hborder,
            t.vborder,
            features,
        ]
    )

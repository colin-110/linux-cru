# Design

## Goal

Make adding a refresh rate on Linux as simple as it is with CRU on Windows:
type a number, apply, reboot.

**In scope for 0.1:**
- automatic timing calculation
- an override that survives reboots
- one-step removal
- Debian/Ubuntu-style systems (GRUB + initramfs-tools)
- a Qt window and a command line tool

**Not yet:**
- other bootloaders (systemd-boot) and initramfs generators (dracut, mkinitcpio)
- applying a mode live without rebooting
- editing other parts of the EDID (colour data, audio, HDR)

**Hard rule:** never leave a machine without a way back. Every change can be
previewed, every replaced file is backed up, and GRUB recovery mode always
boots without the override.

## Why an EDID override?

| Approach | Problem |
|---|---|
| `xrandr --newmode` | X11 only; does nothing on Wayland, which Plasma 6 uses by default. |
| Compositor custom modes | Different (or missing) on each desktop, and usually not persistent. |
| debugfs `edid_override` | Blocked by kernel lockdown when Secure Boot is on, and not persistent. |
| **`drm.edid_firmware`** | Works with any desktop and with Secure Boot, and persists. Needs a reboot. |

The kernel loads the file through the firmware loader, so the file has to be
in the initramfs: the graphics driver often starts before the root filesystem
is mounted.

## Architecture

```
src/linuxcru/
├── core/                 pure logic: no system access, fully unit tested
│   ├── timing.py         Timing type, 18-byte DTD encode/decode
│   ├── edid.py           EDID blocks, checksums, rebuilding with new timings
│   └── calc.py           panel scaling, CVT, CVT-RB, CVT-RB2
├── system/               everything that touches the machine
│   ├── detect.py         connectors in /sys/class/drm, kernel command line
│   ├── install.py        plans and applies installs and removals
│   ├── helper.py         the only code that runs as root
│   └── privileged.py     starts the helper through pkexec
├── cli/main.py           `linuxcru` command
└── gui/                  `linuxcru-gui` (PySide6)
```

### Core

`Timing` stores a mode in DTD units: pixel clock in kHz (multiples of 10),
plus active, front porch, sync and back porch for each direction.
`decode_dtd()` and `encode_dtd()` round-trip byte for byte, including bits
the program doesn't interpret (stereo mode, sync type).

`Edid.with_timings()` rebuilds an EDID around a new list of timings:
- Base-block descriptor slots that held timings (or were unused) get the
  first timings.
- The rest go into the CTA-861 extension block. Its data block collection is
  kept, and the block is created if it doesn't exist.
- Other descriptors are kept as they are (panel name, serial number,
  manufacturer data).
- A Display Range Limits descriptor is only ever widened.
- Every checksum is recalculated.

### Timing methods

- **panel** keeps the panel's totals and scales the pixel clock:
  `clock = h_total × v_total × refresh`. This is what CRU does when you change
  the refresh rate of an existing mode. Laptop panels tend to accept a higher
  clock more readily than different porches.
- **CVT / CVT-RB** follow libxcvt step by step, including its integer
  truncation. Tests compare the results with the `cvt` tool for 30 modes.
- **CVT-RB2** follows the CVT 1.2 formula. The result for 1920×1080 at 60 Hz is
  133.32 MHz, 2000×1111.

### System

`Installer` first builds a `Plan`: a list of `WriteFile`, `RemoveFile` and
`RunCommand` steps. It applies the plan only afterwards. That makes
`--dry-run` trivial, and lets the tests run the real install logic against a
temporary directory with a fake command runner.

The helper runs as root and **re-validates everything**:
- the connector name must match the kernel's naming pattern, so it can't be
  used for path tricks
- the EDID must parse, with valid checksums and at least one timing
- the file must be at most 32 KiB

Root never runs any other part of the program.

### A kernel quirk the installer handles

`drm_edid_load.c` matches `name:file` entries with
`strncmp(connector, name, strlen(name))`, so an override for `DP-1` also
applies to `DP-10` and `DP-1-1`. To handle this:
- the installer refuses a connector name that is a prefix of another
  connector on the system
- when several overrides are installed, longer names are listed first

## Testing

- **Unit tests:**
  - DTD encoding and decoding, checked byte for byte against a real panel's EDID
  - EDID rebuilding, including reproducing an existing tool's output exactly
  - timing math, against reference values and the `cvt` tool
- **Independent validation:** every generated EDID goes through `edid-decode`
  and must not add any problem the factory EDID didn't already have.
- **Installer tests:** run against a fake filesystem that copies a real
  Kubuntu setup, including an existing hand-made override.
- **GUI tests:** run offscreen with Qt's `offscreen` platform.

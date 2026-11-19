# Changelog

## Unreleased

### Fixed
- `linuxcru show` and the window crashed (division by zero) on an EDID with a
  zero-size detailed timing. They now report which timing is invalid.

### Testing
- Property-based tests with hypothesis, including fuzzing of corrupted EDIDs
  and a comparison with the `cvt` tool on random modes.
- Error-path tests and branch coverage, enforced in CI (minimum 90%).
- Manual test plan in `docs/manual-test-plan.md`.

## 0.1.0 (2026-11-13)

First release. Tested on Kubuntu with KDE Plasma 6 (Wayland) on an AUO
B156HAN02.1 laptop panel running at 96 Hz instead of 60 Hz.

- Read EDIDs from `/sys/class/drm` and list their detailed resolutions.
- Timing calculation:
  - keep panel timing (scaled pixel clock)
  - CVT
  - CVT reduced blanking
  - CVT reduced blanking v2
  - manual editing
- Rebuild EDIDs with new modes, keeping all other data and fixing checksums.
- Install and remove boot-time overrides on GRUB + initramfs-tools systems,
  with dry run, backups and a small pkexec helper.
- `linuxcru` command: `list`, `show`, `calc`, `generate`, `apply`, `status`
  and `reset`.
- `linuxcru-gui` Qt window with a CRU-style mode list and timing dialog.

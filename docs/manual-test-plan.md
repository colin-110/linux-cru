# Manual test plan

The automated tests (`pytest`) cover the logic. These cases cover what they
can't: real hardware, rebooting, the password prompt and what the screen looks
like. Run this plan before every release and record the results at the bottom.

**Setup:** a laptop or monitor whose panel can run above its factory refresh
rate, on a GRUB + initramfs-tools system (Kubuntu, Ubuntu, Debian, ...).
Read [recovery.md](recovery.md) first.

## Test cases

### Reading and calculating (no changes to the system)

| ID | Steps | Expected result |
|---|---|---|
| TC-01 | `linuxcru list` | The built-in display is listed first, as connected, with its model name. |
| TC-02 | `linuxcru show` | The detailed resolutions match `edid-decode /sys/class/drm/card*-eDP-1/edid`. |
| TC-03 | `linuxcru calc 96` | A 96 Hz mode using the panel's own porches. A pixel clock warning is shown. |
| TC-04 | Note the times of the files under `/lib/firmware/edid/` and `/etc/default/grub.d/`. Run `linuxcru apply 96 --dry-run --yes`. | The plan is printed. No password is asked for. File times are unchanged. |

### Window

| ID | Steps | Expected result |
|---|---|---|
| TC-05 | Open **linuxcru** from the app menu. | The window opens with the built-in display selected and its modes listed. |
| TC-06 | Click **Add…** and type `96` in Refresh rate. Don't press Enter. | Porches, totals and pixel clock update while you type. |
| TC-07 | Add a mode, move it up, delete it, click **Revert**. | The list and the "preferred" label follow each action. Revert restores the original list. |
| TC-08 | Click **Apply…**, then cancel the password prompt. | "Administrator access was not granted." Nothing changes (`linuxcru status`). |

### Installing and removing (needs reboots)

| ID | Steps | Expected result |
|---|---|---|
| TC-09 | With another tool's override in `/etc/default/grub`, click **Apply…** and accept replacing it. | A backup appears in `/var/lib/linuxcru/backups/`. `/etc/default/grub` no longer contains `drm.edid_firmware`. |
| TC-10 | Reboot after TC-09. | `cat /proc/cmdline` shows `drm.edid_firmware=eDP-1:edid/linuxcru-eDP-1.bin`. The new rate can be chosen in System Settings > Display & Monitor. |
| TC-11 | Use the new rate for 10 minutes: scroll, play a video, move windows. | No flicker, ghosting or black frames. |
| TC-12 | Open the GRUB menu (Esc while booting) > Advanced options > recovery mode, and resume to a normal session. | `cat /proc/cmdline` does **not** contain `drm.edid_firmware`. |
| TC-13 | Click **Remove override…**, confirm, then reboot. | Only the factory modes are offered. `linuxcru status` says nothing is installed. |
| TC-14 | Apply a mode again after TC-13 and reboot. | Same result as TC-10. |

## Results

Record who ran each case, when, and on what hardware. Use **pass**, **fail**
(add a link to the issue) or **not run**.

### 0.1.0: Kubuntu, KDE Plasma 6.6 (Wayland), kernel 7.0, AUO B156HAN02.1 (eDP-1)

| ID | Result | Date | Notes |
|---|---|---|---|
| TC-01 | pass | 2026-11-19 | Found on card1; it was card2 on an earlier boot, and lookup by name still works. |
| TC-02 | pass | 2026-11-13 | Modes match edid-decode. |
| TC-03 | pass | 2026-11-19 | 95.999 Hz, 227.00 MHz, warning shown. |
| TC-04 | pass | 2026-11-19 | No password asked, file times unchanged. Finding: the plan lists files as "would write" even when their content wouldn't change (see issues). |
| TC-05 | pass | 2026-11-13 | |
| TC-06 | pass | 2026-11-13 | Failed first (values only updated after Enter); fixed in 864da85, now covered by automated tests. |
| TC-07 | not run | | |
| TC-08 | not run | | |
| TC-09 | pass | 2026-11-13 | Replaced a drmcru override; backup in `backups/20261113-213835`. |
| TC-10 | pass | 2026-11-13 | Booted with linuxcru-eDP-1.bin, 96 Hz and 75 Hz selectable. |
| TC-11 | not run | | 96 Hz reported working after reboot; the 10-minute check wasn't done. |
| TC-12 | not run | | |
| TC-13 | not run | | |
| TC-14 | not run | | |

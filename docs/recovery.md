# Recovering from a bad EDID override

If a refresh rate is too high for the panel, the screen may stay black,
flicker or show "out of range" after rebooting. Nothing is broken: the
override only applies to normal boot entries, so you can always get back in.

## 1. Boot into recovery mode

1. Turn the computer on, and while it starts:
   - **UEFI systems** (most laptops from the last ten years): tap **Esc**
     repeatedly.
   - **BIOS systems:** hold **Shift**.

   The GRUB menu appears. On a default Kubuntu/Ubuntu install it is hidden,
   which is why you need the key.
2. Choose **Advanced options for Ubuntu** (or your distribution's name).
3. Choose the entry ending in **(recovery mode)**.

Recovery mode boots without the `drm.edid_firmware` parameter, so the display
uses its own EDID.

## 2a. Remove the override with linuxcru

If you get to the desktop, run:

```bash
linuxcru reset
```

then reboot.

## 2b. Remove it by hand

In the recovery menu, choose **root** to get a shell, then run:

```bash
mount -o remount,rw /
rm -f /lib/firmware/edid/linuxcru-*.bin
rm -f /etc/default/grub.d/99-linuxcru.cfg
rm -f /etc/initramfs-tools/hooks/linuxcru
update-initramfs -u -k all
update-grub
reboot
```

## If recovery mode is not available

If GRUB recovery entries are disabled (`GRUB_DISABLE_RECOVERY=true`):

1. In the GRUB menu, select the normal entry and press **e**.
2. Find the line starting with `linux` and delete the
   `drm.edid_firmware=...` part.
3. Press **Ctrl+X** or **F10** to boot. This change lasts for one boot only.
4. Remove the override as in step 2a or 2b.

## Backups

Whenever linuxcru replaces or removes a file, it first copies it to:

```
/var/lib/linuxcru/backups/<date-time>/
```

The folder mirrors the original paths. For example, if you used
`--replace-existing`, the original `/etc/default/grub` is in
`/var/lib/linuxcru/backups/<date-time>/etc/default/grub`.

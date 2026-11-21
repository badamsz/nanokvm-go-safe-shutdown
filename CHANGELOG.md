# Changelog

## 1.1.0 - 2026-11-20

- New: power off the NanoKVM Go itself cleanly (`systemctl poweroff`), so it can be unplugged without corrupting storage.
- Home screen now offers TARGET, NANOKVM, or BOTH. BOTH powers off the KVM only after the target's video is gone.
- KVM power-off is refused while a file transfer or virtual-disk change holds the storage lock, and warns if the data disk is still attached to a running target.
- After a target-only shutdown, the result screen offers to power off the NanoKVM too.
- NanoKVM power-off stays available when the target profile is misconfigured.

## 1.0.0 - 2026-11-20

- First release: one-tap graceful shutdown with confirmation.
- Profiles: windows, windows-dialog, linux-power-key, linux-terminal, macos (experimental), custom.
- Power-off confirmation by watching for the target's video signal to drop.
- Reads the MCP key from the device automatically.

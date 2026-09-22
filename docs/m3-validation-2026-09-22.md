# M3 Air private installation checkpoint — 2026-09-22

This records an owner-operated plain installation on a 13-inch M3 Air (Mac15,12, `apple,j613`, `apple,t8122`, 16 GB RAM), followed by a separately authorized Aurora trial. It is a local development checkpoint, not release qualification. The macOS installer, Recovery handoff and first Linux desktop boot completed. Earlier build-history statements that physical installation was pending are superseded for this exact candidate only.

## Exact candidate identities

| Component | Recorded input |
| --- | --- |
| Desktop | `omacom/omarchy-mac:quattro-upstream`, `1c595bb6030c487c0b584f3e192ef9e8b858b821` |
| Image implementation | `40154c5031ff848c0ce06867c12f9daea136fe13`; preserved builder branch `integrate/quattro-candidate-inputs` at `ecb213674022352253c1ce766a1ae438c6ea427f` |
| Image build | `20260922T012902Z-2828441`; 71 source-test files and 21 installed-system checks passed; exact inventory of 962 packages |
| Signed nine-package candidate | Run `35669668819`; receipt SHA256 `c6cc8904a9ebf950db60ea2917c189e5b1c2c94329ac0fa5a699d3d74672acd3`; package source `5b41ff12fbf97c7fc86a161e58763ebc97e4e2d9` |
| Signed dependency snapshot | Run `35659223153`; manifest SHA256 `cc4d953bf6dc345ed78705bbae4eb8dd8a27d3005dce4fcdd4d5a7567adf472f`, re-read from the retained artifact for this checkpoint |
| Installed desktop/settings | Both `4.0.0.alpha.quattro.r1790015926.g1c595bb6030c-1.356696688190001` |
| Installed add-on | `omarchy-mac 0.1.0-4.356696688190001` |
| Private image | `omarchy-quattro-1c595bb6030c-development.zip`; 4,189,468,939 bytes; SHA256 `161e4273e0885986210b64eb7a9e14756e6bcb3c7a8383f3f58cb43248cdf595` |
| Standalone app | `6f45ad29f75c606a1d0c0657f9f9809cfd23c299` on preserved `fix/m3-inspection-engine`; app ZIP SHA256 `486b24e7f943bbd3c28d588d538f78ca9cd4feb9352875579d54e74ef3a92412` |
| Inspection engine | `.17`, 17,838,045 bytes; SHA256 `ecb61645a9c75ba733425fb300b8b53b09f9dbc297a86acce1e0ee41f36e32e5`; Asahi `dffbb38ef0c00c0431c609ecd8a00f42deb5b24c` (0.9.2), historical overlay `8cb67b490fc8ffb4d9b338759403c18238a1b11a` |

The app fix selects the verified `.17` engine for M3 inspection instead of `.14`. Preserve that archive identity; a moving overlay or source lock is not a substitute. Signed package inputs and a private development catalog do not establish production app/image signing or publication. The complete cached image build took about 11 minutes; packaging the existing images into the installer ZIP took 140 seconds without a Linux rebuild.

## Plain Asahi installation results

With `linux-asahi 7.1.13.asahi1-1`, running `7.1.13-1-1-ARCH`, the owner confirmed desktop startup, Wi-Fi, YouTube playback, explicitly selected mapped microphone recording/playback, one 33-second lid s2idle suspend/resume cycle with Wi-Fi/audio returning, and one normal reboot with playback/capture working. Read-only inspection confirmed successful first-run, expected package ownership and versions, no current failed system/user units, microphone links, actual suspend timestamps and a changed boot ID after reboot. Recorded partition identities matched the installation evidence, including preserved ISC, macOS and Recovery identities.

Plain `parecord` still selects the speaker DSP monitor despite the reported microphone default. Explicit `--device=@DEFAULT_SOURCE@` or `--device=omarchy_asahi_mic.monitor` works. The owner-tested commands were:

```bash
parecord --device=@DEFAULT_SOURCE@ --channels=2 --rate=48000 --file-format=wav /tmp/omarchy-m3-mic-test.wav
# Speak, then stop recording with Ctrl+C.
paplay --device=audio_effect.j613-convolver /tmp/omarchy-m3-mic-test.wav
```

Display was `simpledrm`, 2560×1600 at scale 2, with software rendering. The packaged `appledrm` notch default exists but is not driving this display. GPU acceleration, notch behavior and tearing patches are unqualified. The exact SSH/UFW repair was not captured. Bluetooth HCI, brcmfmac and sleep-lock warnings remain follow-up observations; successful Wi-Fi recovery does not qualify Bluetooth or lock behavior. Camera, encryption, snapshots, return boot to macOS and long-term reliability are unqualified.

## Separate Aurora trial

The exact signed set from `maralcbr/omarchy-pkgs` release `aurora-packages-3caea4693df7c472d57809b5c6b05b07579c1f7a` replaced Asahi normally: `linux-aurora 7.1.12.aurora2-7`, `linux-aurora-headers 7.1.12.aurora2-7`, and `m1n1-aurora 1.6.1.aurora1-2`. No dependency/overwrite bypass or system-trust change was used.

The latest retained check records running `7.1.12-2-7-ARCH`, a clean reboot starting 2026-09-22 09:02:47 America/New_York, connected Wi-Fi, zero failed system/user units, and active audio/microphone/speaker-protection services. The owner confirmed no GRUB error prompt. Audible playback, capture and suspend were not repeated under Aurora. Speaker protection logged an `Invalid sample rate` panic and automatic restart under both kernels; it was active at the latest check.

The first verifier falsely rejected locale-sorted DTBs by comparing them with Python sorting. The corrected read-only verifier matched all 110 DTBs, including `t8122-j613`, and the m1n1/U-Boot/kernel/initramfs/GRUB package inputs, backups and partition table. No boot-image repair was needed. Future `update-m1n1` must explicitly select Aurora DTBs while the higher-version old Asahi modules remain.

The owner applied a separately staged GOP-only GRUB repair (`GRUB_VIDEO_BACKEND="efi_gop"`), with backups and a candidate diff proving only `load_video` changed. Marcelo's runtime PR #209 already supplies the matching source fix. The initial Aurora apply script and old-kernel pre-reboot verifier must not be rerun against the current installed state.

## Evidence retention and next boundary

Detailed records remain outside Git under the local trial root `omarchy-iso-worktrees/quattro-trial/development-payload-20260922T012902Z-2828441/m3-replacement-plan`. The companion [evidence index](m3-validation-evidence-2026-09-22.json) hashes the records read for this summary; it records evidence integrity, not a new authentication trust root. `linux-first-boot-validation.md` contains the chronological owner confirmations; later entries supersede its earlier pending checks. Large archives, audio, host-specific scripts, network identities and credentials are excluded from source commits.

Root backups remain on the M3 under `/var/lib/omarchy-aurora-trial/3caea469`, and the pre-Aurora stage-two image remains `/boot/efi/m1n1/boot.bin.pre-aurora-3caea469`. Preserve the tested source branches and these recovery materials. The active dev-linked checkout remains `quattro-mac-live` at `350c46550b99688cdb5224408edd5870de2ca07b`.

Prepare encryption/Limine as scoped local source changes with original provenance and tests. Keep the `omarchy-mac` add-on kernel-neutral and keep candidate signing/delivery separate from live edge. A future encrypted M3 installation, owner/recovery unlock, reboot, update and snapshot boot/restore require a coordinated pinned candidate and concrete owner review. This checkpoint authorizes no physical operation, publication or repository-trust change.

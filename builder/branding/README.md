# Apple Silicon boot branding

`omarchy-logo.png` is a byte-identical copy of the authoritative Omarchy
project icon. `omarchy-volume.icns` contains verified PNG representations of
that mark for the macOS Startup Options volume. The three `bootlogo_*.bin`
files are the same mark rendered as fixed-size RGBA data for m1n1's supported
48, 128, and 256 pixel boot-logo slots.

`branding-manifest.json` binds every source and derived asset by exact size and
SHA-256 digest. It also binds the exact finalized Asahi `boot.bin` produced
after m1n1/U-Boot assembly, each original Asahi logo region, and the complete
expected branded output. The patcher must reject a different finalized boot
payload or any unexpected bytes before writing.

These assets change product presentation only. They do not rename m1n1, the
Asahi installer, the Asahi kernel work, or any other upstream component, and
they do not alter upstream authorship or provenance.

The Asahi 4.0.3 contract uses the signed m1n1 1.6.1, Linux Asahi 7.1.13,
and U-Boot 2026.07 package inputs. Reassemble with the vendor update-m1n1
algorithm, preserving U-Boot archive timestamps, and verify every original
logo region before updating the full input/output hashes.

Native builds mark the mounted image ESP with `.builder`, which the vendor
helper explicitly honors instead of the physical host device tree. The marker
is removed after package hooks finish and before the release ESP is captured.

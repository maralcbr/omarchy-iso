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

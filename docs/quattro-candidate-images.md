# Signed quattro candidate image inputs

This diagnostic path consumes the three signed packages produced by `omarchy-mac/omarchy-pkgs-aarch64`: `omarchy`, `omarchy-settings`, and `omarchy-mac`. It does not publish packages, update edge, alter the installed desktop, or install onto physical disks. The existing Asahi product and kernel remain selected. Aurora and release qualification are excluded from this first integration.

## Recorded input and trust

Download the `signed-quattro-image-inputs-*` artifact from a successful main-branch candidate run. Select a full desktop source commit and record the SHA256 of `signing.json`. Neither `SHA256SUMS` nor a key downloaded alongside an artifact is an authentication source. The importer verifies the receipt, every named package/list/manifest signature, the exact file hashes, archive metadata, embedded source revisions, and the paired settings version. It copies only those inputs into a private read-only snapshot before the privileged builder starts, then verifies them again inside the builder.

`builder/quattro-trust/public.gpg` and `policy.json` are copied without modification from `omarchy-mac/omarchy-pkgs-aarch64` commit `243494c3c44d5de85ed255f0e6f316a986d0cabc`, paths `pkgbuilds/omarchy-mac-keyring/omarchy-mac.gpg` and `signing-policy.json`. They pin Naeem's existing public CI signing key; no private key is present. Key rotation requires a reviewed trust update here.

The private online build configuration selects a local candidate repository before the older runtime snapshot, with `SigLevel = Required DatabaseOptional`. Candidate trust is added only to the disposable builder's keyring. No candidate repository or candidate key is installed into the target. The existing installed repository configuration remains unchanged. The candidate provenance replaces the older runtime release marker in diagnostic images; it does not claim a released desktop bundle.

## Short test cycle

Use the package check first. It runs the same signature, dependency-download and offline-repository validation stages as the image build, then performs a real pacstrap transaction in a disposable directory root. It installs the three candidates and their runtime dependencies, executes package hooks, and verifies installed versions and source markers. It stops before filesystem image creation, hardware setup, boot finalization and release compression. This is installation evidence, not boot evidence.

Record and reuse the same `SOURCE_DATE_EPOCH` for a candidate; the command below initially takes it from the builder commit. Candidate builds reject a missing epoch before Docker starts.

The host needs Bash 5, Python 3.11+, GnuPG, bsdtar, the existing builder prerequisites, and working Docker access. Use a private disk-backed temporary directory outside the checkout, and preserve the same checkpoint/cache root across runs. Run from this checkout:

```bash
mkdir -p "$HOME/.cache/omarchy-quattro-build-tmp"
chmod 700 "$HOME/.cache/omarchy-quattro-build-tmp"
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) \
TMPDIR="$HOME/.cache/omarchy-quattro-build-tmp" \
  bash bin/omarchy-iso-make \
  --target aarch64/apple-silicon --artifact asahi-os-package \
  --mode diagnostic --no-boot-offer --keep-pkg-cache \
  --candidate-packages /absolute/path/to/signed-artifact SIGNING_JSON_SHA256 FULL_DESKTOP_COMMIT \
  --candidate-package-check
```

A successful check writes `build-evidence/<run>/package-install-check/result.json`, installed package versions and the pacstrap log. Failed transactions, hook errors, missing candidates and mixed versions do not produce a passing result. The first cold run still downloads the dependency set and prepares the toolchain. Warm-run speed must be measured; no duration is promised yet.

Remove `--candidate-package-check` to advance to a complete diagnostic image-root build. Existing diagnostic mode does not emit a release ZIP or installer catalog. An installable development catalog remains a separate step after this path passes, followed by physical testing through the macOS app.

## Cache and timing boundaries

Candidates use an isolated download cache shared across candidate revisions, separate from the normal image build and the host pacman cache. This avoids re-downloading unchanged dependencies for each desktop SHA. Exact candidate receipts participate in the verified-package runtime identity; downstream checkpoints cannot reuse another package set merely because the download directory is shared. The existing lifecycle lease serializes cache mutations.

ARM snapshot archives are retained by their signed SHA256 outside the pruned offline closure. Reuse checks the hash and verifies the signature again; corrupt entries are downloaded again. Focused tests cover reuse after pruning and recovery from corrupted cache bytes. Signed descriptors and signatures are still fetched on each run.

Keep the verified builder-toolchain checkpoint across cycles. Qualification mode deliberately refuses ordinary stage-cache reuse; diagnostic mode is the iteration path. Existing stage evidence reports elapsed time and cache hits. Compare cold and warm package checks first, then configured-target, finalized-boot and compression timings before changing checkpoint contracts. The current base-image identity includes the offline repository, so desktop changes may recreate otherwise empty filesystem images; decoupling that needs a separate proof that all filesystem-tool inputs remain recorded.

## Initial evidence

The first automatic signing run is https://github.com/omarchy-mac/omarchy-pkgs-aarch64/actions/runs/35555243204. Its desktop revision is `fe18cd6ca74ccff9e0bec21ad930ad5186556d2d`; its signed receipt SHA256 is `dd50811d3596e3da36a8b9cb209171256bad8fbcb9ef74e4cae414be5a7e8963`. The artifact has passed local signature and archive validation against the pinned public key. The aggregate suite passed all 68 test files as a non-root user with four workers, including real disposable-key signature tests, candidate selection, package-check failure handling, and installed provenance coverage. The first local Docker trial authenticated the candidates and prepared the toolchain, then exposed Intel/T2-only entries in the shared optional manifest. Candidate Apple selection now explicitly excludes those hardware packages and uses ALARM’s `mise` package for `mise-bin`; unknown required packages still fail resolution. A repeat package-installation trial is in progress. Hardware setup and boot validation remain pending.

## Experimental M3 Air admission

The standalone macOS installer’s pinned Asahi engine (`dffbb38ef0c00c0431c609ecd8a00f42deb5b24c`) already lists `j613ap` and `j615ap`; its release-input template includes `apple,j613` and `apple,j615`. No blanket M3 gate needs removal. The downloaded, pinned `linux-asahi-7.1.13.asahi1-1` archive contains both `t8122-j613.dtb` and `t8122-j615.dtb` under its module DTB directory. These are component checks, not evidence of a successful M3 installation.

The next admission step is a development catalog binding the verified image and engine, followed by an M3 Air boot test. GPU acceleration is not a prerequisite for that experiment. Scott’s optional Hyprland tearing patch can be evaluated after the first boot. Existing unsupported-host restrictions remain in place; availability of the M4 development Mac does not qualify it as an installation target.

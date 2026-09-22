# Signed quattro candidate image inputs

Latest physical evidence: [M3 Air private installation checkpoint, September 22, 2026](m3-validation-2026-09-22.md). The exact private candidate completed installation, Recovery handoff and plain Linux validation; earlier pending statements below describe historical build stages. Encryption and Limine remain unqualified on this M3.

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

A successful check writes `build-evidence/<run>/package-install-check/result.json`, installed package versions and the pacstrap log. Failed transactions, hook errors, missing candidates and mixed versions do not produce a passing result. The first cold run still downloads the dependency set and prepares the toolchain. Recorded local stage timings appear below; network and host differences will affect repeat runs.

Remove `--candidate-package-check` to advance to a complete diagnostic image-root build. Existing diagnostic mode does not emit a release ZIP or installer catalog. An installable development catalog remains a separate step after this path passes, followed by physical testing through the macOS app.

## Cache and timing boundaries

Candidates use an isolated download cache shared across candidate revisions, separate from the normal image build and the host pacman cache. This avoids re-downloading unchanged dependencies for each desktop SHA. Exact candidate receipts participate in the verified-package runtime identity; downstream checkpoints cannot reuse another package set merely because the download directory is shared. The existing lifecycle lease serializes cache mutations.

ARM snapshot archives are retained by their signed SHA256 outside the pruned offline closure. Reuse checks the hash and verifies the signature again; corrupt entries are downloaded again. Focused tests cover reuse after pruning and recovery from corrupted cache bytes. Signed descriptors and signatures are still fetched on each run.

Keep the verified builder-toolchain checkpoint across cycles. Qualification mode deliberately refuses ordinary stage-cache reuse; diagnostic mode is the iteration path. Existing stage evidence reports elapsed time and cache hits. Compare cold and warm package checks first, then configured-target, finalized-boot and compression timings before changing checkpoint contracts. The current base-image identity includes the offline repository, so desktop changes may recreate otherwise empty filesystem images; decoupling that needs a separate proof that all filesystem-tool inputs remain recorded.

## Initial evidence

The first automatic signing run is https://github.com/omarchy-mac/omarchy-pkgs-aarch64/actions/runs/35555243204. Its desktop revision is `fe18cd6ca74ccff9e0bec21ad930ad5186556d2d`; its signed receipt SHA256 is `dd50811d3596e3da36a8b9cb209171256bad8fbcb9ef74e4cae414be5a7e8963`. The artifact has passed local signature and archive validation against the pinned public key. The aggregate suite passed all 68 test files as a non-root user with four workers, including real disposable-key signature tests, candidate selection, package-check failure handling, and installed provenance coverage. The first local Docker trial authenticated the candidates and prepared the toolchain, then exposed Intel/T2-only entries in the shared optional manifest. Candidate Apple selection now explicitly excludes those hardware packages and uses ALARM’s `mise` package for `mise-bin`; unknown required packages still fail resolution. The real package-installation trial passed at builder revision `dcdbe99`: the three exact candidates installed together, hooks completed, and installed versions/source markers matched. Its run is `20260921T034443Z-1258049`; `package-install-check/result.json` records a 31-second transaction. The warm verified package-cache stage took 145 seconds versus 568 seconds with the first dependency download; offline repository restoration was a cache hit. These are stage timings, not end-to-end installation timings. All 68 source-test files also passed at `dcdbe99`. The subsequent full diagnostic image build resolved 968 target packages and completed their installation, then stopped during `omarchy-apply-system` at Snapper setup. In a chroot, `systemctl cat limine-snapper-sync.service` returns success while ignoring the command, so the runtime tries to enable an absent Limine service on the GRUB image. Local desktop fix `939b0d85` on `fix/snapper-chroot-unit-detection` queries installed unit files offline instead. Its focused Snapper tests and real systemd absent/present-unit probe pass. The desktop aggregate suite also passes at `939b0d85` (CLI plus all 287 shell test files) with `NO_COLOR` and `LC_ALL` unset; the initial inherited environment caused two unrelated fixture failures, both resolved by that clean invocation. A new signed candidate containing that fix is required before continuing image qualification; the importer must not silently patch the signed runtime. No complete image, installer catalog, or boot evidence is claimed.

## Experimental M3 Air admission

The standalone macOS installer’s pinned Asahi engine (`dffbb38ef0c00c0431c609ecd8a00f42deb5b24c`) already lists `j613ap` and `j615ap`; its release-input template includes `apple,j613` and `apple,j615`. No blanket M3 gate needs removal. The downloaded, pinned `linux-asahi-7.1.13.asahi1-1` archive contains both `t8122-j613.dtb` and `t8122-j615.dtb` under its module DTB directory. These are component checks, not evidence of a successful M3 installation.

The next admission step is a development catalog binding the verified image and engine, followed by an M3 Air boot test. GPU acceleration is not a prerequisite for that experiment. Scott’s optional Hyprland tearing patch can be evaluated after the first boot. Existing unsupported-host restrictions remain in place; availability of the M4 development Mac does not qualify it as an installation target.

## Local continuation

The isolated image-builder branch is `integrate/quattro-candidate-inputs`. Nothing from this integration has been pushed or published. Trial logs and receipts are under `/home/scott/code/omarchy-iso-worktrees/quattro-trial`; source test ledgers are retained in the checkout’s ignored `test-runs/` directory. The successful short trial is `20260921T034443Z-1258049`; the full target failure is in `diagnostic-image-loops-ready.log`. All trial containers and file-backed loop devices were cleaned up.

Review the separate Snapper fix, include it in the protected desktop branch, and let the candidate workflow produce a new signed set. Record the new source SHA and receipt hash, then repeat the short transaction and diagnostic image commands with the same persistent cache and `SOURCE_DATE_EPOCH=1789301403`. Only after target configuration and boot-file validation pass should the development catalog and macOS installer consumption be wired up for a physical test.

On this Linux Docker host the first loop attachment reported a missing `/dev/loop0`; a fresh container subsequently exposed the kernel’s loop nodes and an isolated attach/detach probe passed. The next diagnostic run successfully created and mounted the image. This was an environment startup issue, not evidence that physical boot works.

## Post-merge check: 2026-09-21

PRs omacom/omarchy-mac#493 and #494 are merged in desktop revision `1c595bb6030c487c0b584f3e192ef9e8b858b821`. Candidate workflow run `35639858283` built and signed all three packages successfully. Its receipt SHA256 is `b8b97d4f755039e16e0b848f0d4f47f8644c91866ae1fbeeb1e4eec153dfa65c`. Local signature validation passed, and inspection of the authenticated runtime archive confirmed the Snapper and both raw LUKS lookup fixes.

The short transaction passed in run `20260921T190609Z-1975436` in 31 seconds, with exact installed versions and source markers. The subsequent diagnostic image again installed its full target package set and this time completed `install/config/snapper.sh`, proving the merged fix resolves the observed chroot blocker.

Full image setup then failed in `install/login/grub-splash.sh`: it invokes `mkinitcpio -P` while the configured stage has deferred preset generation until boot finalization (`No presets found in /etc/mkinitcpio.d`). Apple audio setup also reported `target not found: rtkit` and an incomplete protected audio stack. Hardware setup fetched video-decoding packages through the installed online repository configuration, so complete offline hardware-package closure is not yet established either. Resolve the setup/finalization ordering and hardware dependency/repository inputs before another image qualification attempt; do not treat the package transaction as full image success.

Logs: `/home/scott/code/omarchy-iso-worktrees/quattro-trial/package-check-35639858283.log` and `diagnostic-image-35639858283.log`. The failed image run cleaned up its container and file-backed loop devices. No physical install, dev-link change, image publication, or encrypted owner-setup test was performed.


## Kernel-preset and audio follow-up: 2026-09-21

Local commits `34cfc63` and `99da17e` stage the Asahi kernel and preset before the desktop system finalizer and include `rtkit` in the recorded Apple package targets. Boot finalization still rebuilds the initramfs after hardware setup. Regression coverage checks both supported kernel selections, the preset's availability when runtime setup starts, and failure before setup when the kernel is missing. All 68 portable test files passed as a non-root user; the ledger and logs are `test-runs/preset-fix.json` and `test-runs/preset-fix.log`.

The diagnostic rebuild used the same authenticated desktop revision and receipt recorded above. `diagnostic-image-preset-fix.log` confirms that Snapper configuration, Apple audio setup, and GRUB splash/initramfs setup all completed. The configured-stage capture then rejected `avd-fw` because hardware setup had fetched it from edge outside the verified offline repository. This is a successful rejection of an unrecorded dependency, not a completed image or boot test. The disposable container was removed after the failure.

Two optional video-decoder dependencies need a delivery decision before the next build. The edge release currently provides these archives without detached signatures:

- `avd-fw-0.1-1-any.pkg.tar.xz`, SHA256 `1e45a05995f7114b342c4e562108a06998a5bbfdb889ac4eee46f00895cd4f16`.
- `libva-v4l2_request-avd-1.3-1-aarch64.pkg.tar.xz`, SHA256 `95954647e1f3fb818e9ee1d90d23fc2d2f88c6b70be14cd3462a2aa801179199`.

These hashes identify the observed GitHub release assets; they do not satisfy the image builder's package-signature contract. Prefer candidate-only builds and signatures for these dependencies, followed by explicit inclusion in the offline target set. Alternatively, explicitly defer optional video acceleration for the first trial through a tested installer interface. Do not bypass the image ownership/signature checks or silently treat mutable edge downloads as verified inputs.

M3 host readiness still needs its SSH address, exact model/macOS version, free space, and backup confirmation. No physical M3 installation, image publication, encryption trial, or active desktop change has occurred.

## Five-package candidate integration

The image importer accepts schema-2 candidates containing exactly the desktop trio plus `avd-fw` and `libva-v4l2_request-avd`. All eight named inputs (five packages, two package lists, and the build manifest) and the receipt require signatures from the existing pinned signing key. Desktop source markers must match the selected desktop commit; video source markers must match the recorded package-repository revision. Schema-1 triples remain importable for reproducing older checks, but do not supply the video dependencies needed by current hardware setup.

Every authenticated package name is added to the offline target set, so the image installs the signed decoder packages before hardware setup. The short transaction check also installs the full candidate set and verifies both kinds of source marker. No unsigned-package exception or edge repository change is introduced. Package workflow review: https://github.com/omarchy-mac/omarchy-pkgs-aarch64/pull/56.

PR #56 merged as `511abe844206dde9551192e22d092a648c4c1cbd` after the hosted ARM build and repository self-tests passed. Main run `35652929576` built and signed all five packages successfully. The downloaded receipt hash is `be6a2905aabe7c25f685f8d8a0e029153c9457894cb8fe5c27405816cbfa12e3`; local verification against the existing pinned key passed. The desktop source remains `1c595bb6030c487c0b584f3e192ef9e8b858b821`, and both video archives record the merged package-repository revision. The local image integration is `5a88719`; all 68 portable test files passed (`test-runs/five-package-inputs.json`). These are signed build inputs, not physical boot evidence.

## Signed custom-dependency snapshot trial

`--candidate-dependencies DIRECTORY MANIFEST_SHA256` accepts the independently signed dependency snapshot only alongside diagnostic Apple candidate inputs. It verifies and freezes the manifest, origin database, every archive signature, metadata and hash before mounting a private copy. The dependency manifest participates in the package-cache identity; this path has a separate cache and bypasses the inherited custom repository entirely. The pinned Asahi platform and dated ALARM repositories remain the platform inputs.

The first dependency snapshot is run `35659223153` from `omarchy-mac/omarchy-pkgs-aarch64`, with 50 packages and manifest SHA256 `cc4d953bf6dc345ed78705bbae4eb8dd8a27d3005dce4fcdd4d5a7567adf472f`. Its origin edge database SHA256 is `b3453594f0892b84e9b558c45193e3cd4e083304cb4a3b09c1f09ac5d12fd5a3`. Capture, signing, hosted public-only verification, and local manifest/signature/hash validation passed. No edge publication occurred.

The first complete resolver attempt found missing `asdcontrol`, `tobi-try`, and `qemu-user-static-binfmt` (which requires `qemu-user-static`). All four subsequently built locally from recorded recipes. Package PR [#61](https://github.com/omarchy-mac/omarchy-pkgs-aarch64/pull/61) adds them to the artifact-only schema-3 candidate set. The image importer accepts exactly nine packages for that schema and retains the older three/five-package schemas for existing evidence. Candidate and dependency names must be disjoint. Explicitly select `dotnet-runtime-bin` on this path to avoid pacman's default obsolete .NET 2.1 provider; retain `mise-bin` from our snapshot.

The short transaction installs the complete filtered base list and editor package with the candidates. The configured-target verifier accepts installed virtual providers such as `neovim` providing `nvim`, while kernel/platform/desktop roles still require their exact package names and the entire installed closure must match recorded versions.

Finalization records both candidate and dependency manifests, removes inherited runtime-release claims, and selects the existing `omarchy-aarch64` feed plus Asahi and ALARM for the trial system. That custom feed remains `Optional TrustAll` because live edge is still unsigned; build-input signatures are mandatory and separate. This does not convert live edge or bootstrap trust on any existing installation. The dependency-snapshot path does not install Marcelo's custom repository or its signing key.

Portable suite: 69 test files passed after the initial integration and again with the nine-package importer and full-base transaction checks. Full image creation and physical installation remain pending the complete signed candidate transaction.

## Pinned ALARM Hyprland repair

The September 10 ALARM snapshot has `hyprland 0.56.1-3` requiring `libaquamarine.so=13-64`, but its Aquamarine provides ABI 14. The explicit candidate-dependency path overlays the official ALARM `hyprland 0.56.2-3` rebuild, which requires ABI 14. `builder/quattro-hyprland-repair.json` records the exact archive, SHA256, embedded detached signature, expected ALARM signer, version and dependency. The fetcher verifies all of these before adding the package to the private build repository. Both files participate in the package-cache source identity. The normal repository path and live edge remain unchanged.

The official archive was verified locally against the ALARM keyring and pinned signer. Package PR #61 has merged; signed nine-package run `35669668819` is the candidate used for the repaired full-base transaction.

## Completed Quattro diagnostic image (September 21, 2026)

The full image build passed at builder `40154c5031ff848c0ce06867c12f9daea136fe13`, run `20260922T012902Z-2828441`. It uses desktop `1c595bb6030c487c0b584f3e192ef9e8b858b821`, the nine signed packages from run `35669668819` (receipt SHA256 `c6cc8904a9ebf950db60ea2917c189e5b1c2c94329ac0fa5a699d3d74672acd3`), dependency snapshot `35659223153`, and the pinned official ALARM Hyprland repair. The prior complete package transaction passed in run `20260922T004727Z-2556848`, taking 118 seconds for installation.

The completed image passed exact installed inventory validation for 962 packages, system setup, boot finalization and all 21 installed-system configuration checks. The source suite passed all 71 test files (`test-runs/quattro-final-verifier.json`). Root and boot images, EFI files, checksums, provenance and verification evidence were exported and every exported image/file checked against its checkpoint hash under `/home/scott/code/omarchy-iso-worktrees/quattro-trial/diagnostic-image-20260922T012902Z-2828441`. The root is a sparse 16 GiB image; boot is 1 GiB. This is diagnostic build evidence: no release ZIP or installer catalog was produced, nothing was published, and encryption, first boot and physical hardware remain untested for this image.

Two integration corrections were needed beyond package resolution. The target inventory model now includes archinstall's complete PipeWire backend and its already-installed LV2 provider; the earlier one-shot resolver incorrectly selected Ardour and omitted `pipewire-alsa`. The final verifier has an explicit Quattro profile for the shared package trio and aarch64 feed, and checks NetworkManager's effective configuration from the target chroot instead of requiring the historical `/etc` Wi-Fi leaf. Tests retain the original profile and reject conflicting Wi-Fi overrides, inherited fork repositories and a missing add-on.

The successful run took approximately 11 minutes end to end with downloads cached. Recorded stage work includes 188 seconds verifying package inputs, 138 seconds building the offline repository, 136 seconds configuring the target and 9 seconds finalizing boot; hashing, transfer and host preparation account for additional time. Image checkpoint reuse is intentionally disabled by the current admission adapter. Rebuilding base Btrfs images into an existing identical checkpoint identity can fail because their bytes differ; this run used the fresh `asahi-checkpoints-quattro-final-verifier` namespace while preserving the package download cache and earlier evidence. Reproducibility and checkpoint admission remain separate work, not claims established by this successful build.

## Private installer-format payload

Diagnostic build `20260922T012902Z-2828441` was packaged locally as `omarchy-quattro-1c595bb6030c-development.zip` (4,189,468,939 bytes; SHA256 `161e4273e0885986210b64eb7a9e14756e6bcb3c7a8383f3f58cb43248cdf595`). Compression and complete package verification took 140 seconds using libarchive ZIP/deflate level 1. No Linux rebuild was needed. The private product and generated `installer_data.json` declare the actual 16 GiB root, 1 GiB boot and 500 MiB EFI partition; the bundled production product and installer metadata remain unchanged.

The existing package verifier streamed every member, checked CRCs, ext4/Btrfs signatures, AArch64 EFI and branding, and the packaging wrapper matched archive member hashes to the exported diagnostic build. Installer overlay revision `0cf97ad9e278405982388c503f3ed53d12f3299e` accepted the real ZIP and metadata through `load_metadata` and `_validate_full_os_package`, and rejected a wrong filename and undersized root capacity. That read-only contract check used the checked-in test shims for upstream imports; it did not call preflight, access disks or establish full engine/macOS qualification.

Artifacts, checksums, recipes and evidence are preserved at `/home/scott/code/omarchy-iso-worktrees/quattro-trial/development-payload-20260922T012902Z-2828441`. This remains an unsigned, unpublished diagnostic derivative, not a sealed release or catalog-admitted candidate. The next step is a separately reviewed private development-catalog path and macOS validation; physical installation has not occurred. Scott has confirmed the M3 Air is fully backed up.

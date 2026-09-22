# Apple encryption/Limine image integration

The local branch `integrate/quattro-encryption-limine` integrates the runtime and package source ports into the existing builder. The schema-4 image assembly guard remains enabled. No new encrypted/Limine image, installation, VM boot or release has been qualified.

The preserved M3 builder is `ecb213674022352253c1ce766a1ae438c6ea427f`, with validated implementation `40154c5031ff848c0ce06867c12f9daea136fe13` and checkpoint `cc0bbb5`. Its `integrate/quattro-candidate-inputs` worktree and Chris's frozen private tester archive are unchanged. See [physical evidence](m3-validation-2026-09-22.md).

## Source and trust boundaries

Marcelo's runtime PR [#220](https://github.com/maralcbr/omarchy-mx-mac/pull/220) and package PR [#194](https://github.com/maralcbr/omarchy-pkgs/pull/194) merged on September 22. Their reviewed heads remain pinned to `d418ab7f95e8ba447df4fb368ddd838a5ffc7943` and `68a61cef1aba768c6aae20a0feda2a42e19de6e8`; the runtime pin includes #219 at `5e7a409fae1ddc17433d9408e15153b4fe813f7b`. No excluded monorepo image helper or existing-user migration was imported.

The candidate importer authenticates schemas 1–4, including all thirteen schema-4 packages, source provenance, payload ownership and the exclusion of the upstream repository key. The full package-cache initializer now performs candidate authentication and schema-4 refusal before any pacman-key call or dependency preparation. Previously its dependency preparation ran before the candidate gate; local commit `e8f88a2` fixes that ordering and tests the actual initializer.

Build-input signatures use the existing pinned candidate trust policy. Installed-system feed trust remains separate. Nothing imports Marcelo's repository key, publishes a release, changes the active desktop or alters a physical disk.

## Coordinated package selection

Local commit `102d4c1` adds explicit dependency schema 2, selected only by an authenticated schema-4 candidate. Its contract is `boot_profile=limine`, `candidate_schema=4`, and exactly fifteen exclusions: the thirteen candidates plus `omarchy-apple-boot` and `omarchy-first-boot`. Existing dependency schema 1 retains its five-package exclusion contract and cannot silently authorize the new profile.

Candidate, dependency, platform and additional pinned packages must have disjoint names. The one declared replacement is schema 4's candidate U-Boot replacing the older U-Boot in the unchanged eighteen-package Asahi platform snapshot. The platform keyring also participates in overlap rejection. Every selected candidate is required, legacy boot packages are rejected, and schema 4 retains the Limine hooks that the GRUB package filter previously removed.

The separate [Limine lock](../builder/quattro-limine.json) binds Arch Linux ARM `limine-12.9.0-1-aarch64.pkg.tar.xz` to SHA-256 `abadb32ec9fb6dfa70ba224f3b90435b93ea39db91dc2e838862c620f33fd266`, its detached signature, exact metadata and signer `68B3537F39A313B3E574D06777193F152BDBE6A6`. Its runtime dependency is `glibc`. The actual archive was checked with the existing ALARM public keyring and the new fetcher without modifying installed trust. If the dated package disappears from the mirror, the build fails instead of substituting a newer version. This additional lock and the profile's signed manifests participate in cache identities.

## Opt-in image finalization

Only authenticated schema 4 generates `apple-boot-profile.json` in the finalized runtime projection. The default remains the existing GRUB profile. The dedicated Apple finalizer uses the runtime activation leaf after the final mkinitcpio operation, verifies ARM64 loader bytes against the installed Limine package, validates the UKI's kernel/initramfs/os-release/command-line sections and menu target, and preserves an empty ESP `omarchy` staging directory.

A completed fresh image writes `/var/lib/omarchy/mac-first-boot/pending` and meaningful `deferred-steps` together with Limine activation state. Separate owner provisioning remains armed by `provisioning/pending`, with the pinned offline Node archive. The shipping root and factory snapshot lose builder identity and private pacman keyring state; the factory snapshot also loses fresh-conversion permission, owner pending state and staged keys. These marker writers now exist in the gated finalizer, rather than being left for the next integrator.

Installed content and configuration evidence consume the persisted profile and validate the actual Limine artifacts. The installed-system command uses the runtime boot checker and the installed kernel detector for this profile, and accepts an encrypted root only when its crypt mapper is directly backed by NVMe. Existing GRUB evidence remains the default.

## Validation and remaining qualification

Focused checks passed: 32 signed candidate importer tests, 17 signed dependency/composition tests, three checkpoint-lock tests, package-cache boundary and package-filter tests, nine existing content-capture tests, four new Limine content/config tests, all 38 stage-input/cache-identity tests, and the installed configuration and live-verifier fixture suites. The finalized profile selector is tested from the immutable candidate snapshot independently of any disposable verification scratch directory. Signing fixtures use disposable keys. These source and artifact fixtures do not qualify a real boot.

Before removing the guard:

- Produce and authenticate the complete thirteen-package candidate and matching schema-2 dependency bundle, then run the complete native package transaction and ownership/removal-hook checks against that exact closure.
- Build the new U-Boot/kernel/m1n1 combination and replace the old whole-image branding pin only with measured, provenance-bound output. The current branding lock and permitted release-product boot backend still describe the validated GRUB baseline; the new product contract must be derived from that qualified output.
- Build and inspect one complete private image, including the sealed factory snapshot, marker contents, offline Node inventory, EFI/menu/UKI bytes, cleaned trust and identity state, and package/cache evidence.
- Wire the VM harness to authenticated inputs and exercise plain boot, conversion and second encrypted boot. Upstream payload mode is unauthenticated, and a generic VM cannot qualify Apple firmware or target models.
- Keep the application and installer engine contract unchanged; perform separately authorized hardware qualification before presenting the new profile as ready for testers.

The desktop worktree's `docs/quattro-encryption-limine-source-port-2026-09-22.md` and verification ledger carry the coordinated runtime/package/image record. Its disposition map preserves original attribution and documented adaptations.

## Final source checkpoint

The complete VM-free image aggregate passed all 74 test files with disposable signing fixtures and a disk-backed temporary directory. This includes 17 dedicated Apple finalizer cases, actual cpio listing and activation-link checks, effective Limine maintenance-hook ownership, mounted `@log` cleanup, and synthetic UKI mutation fixtures. The authenticated native Limine ARM64 loader was also inspected as a PE image. These checks do not qualify a complete image or boot.

Two failures were corrected before the green aggregate: an older package-install fixture omitted its candidate schema, and repeated import discovery pushed the source-impact CLI above its existing two-second limit. The fixture now declares the legacy schemas and checks the complete thirteen-package profile and rejection of both replaced boot packages. Commit `af03af5fb0af390c0687671704c34603db921bf7` caches direct import discovery within one validation call; entrypoint declarations remain independent and edits are rediscovered on the next call. Its 38 stage-input and 14 source-impact tests pass, and the measured CLI time fell from about 2.01 seconds to 0.56–0.60 seconds without relaxing the timeout.

The [retained aggregate ledger](/home/scott/code/omarchy-integration-evidence/encryption-limine-image-integration-20260922/unit-test-results.json), prior failure logs and [performance evidence](/home/scott/code/omarchy-integration-evidence/encryption-limine-image-integration-20260922/source-impact-performance/verification.json) distinguish the failures, fixes and final result. The coordinated runtime record pins this final image implementation together with runtime `12308e1ca57d35abf38ff801df589da5e4754823` and package `c71c8c93180d05c1c96e6d560dcfb4b35c627bb6`.

# Image encryption/Limine integration preparation

Base: M3 checkpoint `cc0bbb5`, following preserved builder `ecb213674022352253c1ce766a1ae438c6ea427f`; implemented build source `40154c5031ff848c0ce06867c12f9daea136fe13`. The original `integrate/quattro-candidate-inputs` remains unchanged. See [physical evidence](m3-validation-2026-09-22.md).

Translate package PR #194 into this builder; do not replace this pipeline with `bin/build-mac-image`. Carry final activation after mkinitcpio, builder-identity/history removal, empty ESP staging directory, actual Limine loader bytes, menu path/cmdline and UKI section verification. Runtime #220 must accompany it. Review `builder/asahi-stages/finalized-boot.sh`, configured/finalized orchestration and source-input declarations, `verify-asahi-os-package.py`, `verify-asahi-installed-system.py` and installed inventory together.

Extend candidate schema and package closure in step with the package producer; preserve nine-package receipt compatibility, dependency disjointness, exact source/version records, signature checks and the pinned public trust files. Package recipe versions in the central inventory are not a complete authenticated binary lock; resolve the exact Limine/kernel/m1n1/U-Boot and transitive package set before building.

Use focused candidate/dependency/transaction/finalization tests, then the aggregate suite after a complete slice. Changed runtime/settings and boot inputs invalidate configured target, initramfs/UKI/ESP, ZIP and catalog identities. Reuse verified downloads, not stale downstream checkpoints. Adapt `test/vm/mac-image/run` to verify our private inputs before its plain, conversion and second encrypted boot lanes; the upstream local payload mode has no authentication and the generic guest does not qualify Apple boot hardware. No image rebuild or VM execution has been performed on this preparation branch.

This branch is a local source-preparation branch. No functional port or new build has been performed. The complete dependency inventory and 168-file disposition map are in the desktop repository, branch `integrate/quattro-encryption-limine`, under `docs/quattro-encryption-limine-{integration,sources,files}-2026-09-22.{md,json,json}` (three separate files). The local desktop worktree is `/home/scott/code/omarchy-worktrees/quattro-encryption-limine`.

The proposed runtime source ceiling is `maralcbr/omarchy-mx-mac` open PR #220 at `d418ab7f95e8ba447df4fb368ddd838a5ffc7943`, including merged #219 at `5e7a409fae1ddc17433d9408e15153b4fe813f7b`. The package/image reference is open `maralcbr/omarchy-pkgs` PR #194 at `68a61cef1aba768c6aae20a0feda2a42e19de6e8`. These heads were verified with GitHub API on September 22. Preserve pins and original attribution; neither open PR is a qualified downloadable candidate.

Keep candidate packages/images private and signed under the existing build-input policy. Preserve the active desktop, unrelated dirty worktrees, installed-user feed and repository trust. No publication, remote update, physical disk operation or boot-policy change is part of this preparation.

## Source checkpoint

The signed candidate importer now verifies the explicit thirteen-package schema 4 as well as existing schemas 1–3. It checks the complete set, source provenance, signatures, payload ownership, ARM64 Limine template and conversion owner, and excludes the upstream repository key. Thirty-one offline importer tests passed with disposable signing keys.

Image assembly deliberately refuses schema 4 immediately after authentication, before importing candidate trust or installing packages. This is a draft boundary: finalization, the disjoint dependency/platform closure, first-boot and factory-snapshot state, cache input declarations and authenticated VM lanes are not implemented yet. No new image has been built. The existing M3 baseline and schemas 1–3 retain their behavior. See the central runtime `docs/quattro-encryption-limine-source-port-2026-09-22.md` for exact scope and remaining qualification.

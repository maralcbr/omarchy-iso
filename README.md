# Omarchy ISO

The Omarchy ISO is the only supported way to install Omarchy. It ships the Omarchy Configurator, installs Arch Linux, installs the Omarchy packages from the bundled mirror, runs target system setup in the chroot, creates the user, and runs `omarchy-setup-user` for that user.

## Downloading the latest ISO

See the ISO link on [omarchy.org](https://omarchy.org).

Every published ISO has a `.sha256` beside it at the same URL. Download both into the same directory and check the ISO before writing it to a USB stick:

```bash
sha256sum -c omarchy-3.0.iso.sha256
```

Corruption anywhere in the ISO is worth catching before the write, and corruption in the bundled package mirror is worth catching most: the mirror lives inside the ISO and the installer reads it straight off the medium, so those bytes surface minutes into the install as a pacman "invalid or corrupted package" error rather than as anything that names the download. Corruption elsewhere is louder and earlier — it stops the medium booting or mounting. There is a `.sig` beside the ISO too for anyone who wants to verify it against the Omarchy signing key.

## Creating the ISO

Run `./bin/omarchy-iso-make`; output goes into `./release`. By default the ISO uses the Omarchy packages and tracks the `quattro` branch, from the stable mirror. Pass `--edge` to use `omarchy-dev` and `omarchy-settings-dev` from the edge mirror.

The build defaults to the canonical `x86_64/pc` media target. Pass
`--arch aarch64` or `--target aarch64/generic` to select generic UEFI ARM64.
The distinct `aarch64/apple-silicon` target is defined but currently fails
before the build. `builder/apple-platform-snapshot.json` pins nine current
Asahi platform packages and detached signatures by SHA-256 and records the
verified signing-key fingerprint. The Apple-only offline fetch path verifies
and adds those packages to the mirror, and its live profile selects
`linux-asahi` plus the Asahi initramfs firmware hook. The bridge-owned internal
boot bundle supplies model device trees and U-Boot; the removable media retains
the standard `/EFI/BOOT/BOOTAA64.EFI` boundary. A complete Apple ISO build and
boot through that chain have not been proven, so the target remains disabled.
It will never silently substitute generic ARM media; architecture selection
alone does not make an Apple Silicon image.

For disposable build validation only, the explicit combination
`--target aarch64/apple-silicon --apple-media-validation-build` bypasses the
readiness stop while retaining the Apple target identity. It uses a separate
package cache and names the result `*-aarch64-apple-silicon-*.iso`, preventing
it from colliding with generic ARM media. The command labels the output
unverified and forbidden for release; it does not satisfy the build-and-boot
gate by itself.

For local development, build the ISO from sibling checkouts:

```bash
./bin/omarchy-iso-make --local-source ../omarchy-installer ../omarchy-pkgs
```

Despite the local folder name, the first argument is the Omarchy source checkout (runtime commands, configs, setup scripts, themes, shell, migrations). The installer itself lives in this ISO repo.

Use `--dev` or `--rc` to build against those package channels. Both `--dev` and `--edge` select the dev packages from the edge mirror.

## Autoinstall

The shipped ISO installs itself with no keyboard when it finds its configuration on a second drive. Attach a drive labeled `cidata` alongside the ISO and the installer copies the config off it and skips the configurator; with no such drive, nothing changes and the wizard runs as usual. No rebuild, no extra boot entry.

`cidata` is the cloud-init `NoCloud` label, so Proxmox, libvirt, and Packer already know how to attach one.

### Configuration files

These are the configurator's own output files, so the way to get a starting set is to run one interactive install and copy what it wrote into `/root`.

| File | Required | Purpose |
|------|----------|---------|
| `user_configuration.json` | Yes | archinstall config: disk, hostname, timezone, keyboard |
| `user_credentials.json` | Yes | Username and password hash |
| `user_full_name.txt` | No | Git full name |
| `user_email_address.txt` | No | Git email |
| `user_encrypt_installation.txt` | No | `true` when `user_configuration.json` carries a `disk_encryption` block; defaults to false |
| `authorized_keys` | No | SSH public keys in sshd's own format, one per line |
| `tailscale_authkey` | No | Tailscale auth key; the machine joins your tailnet on first boot |

Both required files must be present or the installer falls back to the configurator. Generate the password hash for `user_credentials.json` with `openssl passwd -6 "yourpassword"`.

Encryption itself is configured by the `disk_encryption` block inside `user_configuration.json` — which carries the passphrase in plaintext, so treat a drive built from an encrypted install accordingly. The flag file must match it: it drives the encrypted install's SDDM autologin and the final boot validation, not the encryption.

`authorized_keys` is the same file sshd reads — copy your own or write one key per line:

```
ssh-ed25519 AAAA... you@host
```

When `authorized_keys` is present, autoinstall installs it as the user's `~/.ssh/authorized_keys`, enables `sshd`, and adds a `ufw allow ssh` rule — a stock Omarchy install ships openssh with the service disabled and its firewall opens neither port 22 nor anything else beyond LocalSend. Networking needs nothing extra; NetworkManager is already enabled with DHCP. Password SSH authentication is left at the distro default. An `authorized_keys` with no usable keys fails the install rather than producing a machine nobody can reach.

When `tailscale_authkey` is present (one key, blank lines and `#` comments ignored), the install adds the `tailscale` package from the ISO's bundled mirror — nothing is fetched from the network at install or boot — and stages the join for first boot: the key lands at `/etc/tailscale/authkey` (root-only), `tailscaled` is enabled, ufw allows traffic in on `tailscale0`, and a background unit runs `tailscale up` once the network is actually up, retrying until it succeeds without holding up the boot. After a successful join the key is deleted and the unit disables itself; until then both survive reboots, so a machine installed offline joins whenever it first gets connectivity. The node appears on the tailnet under the configured hostname. Use a reusable, pre-authorized (tagged) key so one drive image serves many machines — or an ephemeral key for disposable VMs.

### Building the drive

```bash
mkdir cidata
cp user_configuration.json user_credentials.json authorized_keys cidata/
genisoimage -output cidata.iso -volid cidata -joliet -rock cidata/
```

### Proxmox example

```bash
qm create 101 --name my-omarchy \
  --bios ovmf --machine q35 --cpu host --cores 4 --memory 8192 \
  --ostype l26 --scsihw virtio-scsi-single \
  --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0 \
  --scsi0 local-lvm:40,discard=on,iothread=1 \
  --net0 virtio,bridge=vmbr0 --vga virtio --serial0 socket \
  --ide2 local:iso/omarchy.iso,media=cdrom \
  --ide3 local:iso/cidata.iso,media=cdrom \
  --boot order='scsi0;ide2'

qm start 101
```

Boot order is disk first: the empty disk falls through to the ISO on the first boot, and the installed system boots from disk afterwards. The machine reboots into Omarchy on its own when the install finishes.

Encrypted autoinstalls are not fully unattended — the LUKS passphrase prompt still needs someone at the first boot.

## Testing the ISO

Run `./bin/omarchy-iso-boot [release/omarchy.iso]`.

Run `./test/all` for the fast, VM-free tests under `test/unit/`, which cover cidata autoinstall loading and the orchestrator's phases without needing a built ISO.

To exercise installation alongside existing Windows-style partitions, run
`./bin/omarchy-iso-test-windows-disk [release/omarchy.iso]`. It creates a
synthetic disk in `/tmp` with an existing ESP and data partition plus ample
unallocated space, then offers to start an interactive installation on it. The
fixture exercises Windows partition preservation but does not contain Windows.

## Acceptance testing the ISO

Run `./bin/omarchy-iso-test [release/omarchy.iso]` to install the ISO into a headless VM by driving the real interactive install flow — the harness reads each screen via QMP screendumps + OCR and answers with virtual keystrokes, so the configurator wizard, install dashboard, reboot prompt, and SDDM login are all exercised exactly as a user would. It then boots the installed system, sends real VM keyboard shortcuts for the primary shell and window-management actions, and runs the in-guest acceptance suite (`test/acceptance` in the omarchy repo). The suite checks session and service health, the complete core-package manifest, user defaults, representative applications, menus, panels, live weather, launchers, visual selectors, notifications, clipboard, and other interactive shell behavior.

Visual checkpoints are saved as `success-<step>.png` or `failure-<step>.png` alongside the serial and install logs in `test-runs/<iso>/runs/<timestamp>/`. Independent test files and applications continue after a failure so one broken surface does not hide the rest of the report. The harness then stops the VM and opens the ordered screenshots in `imv` for quick visual review.

The harness syncs the acceptance suite from `$OMARCHY_PATH` when it is available. The install phase produces a reusable base image, so iterating against another checkout is fast:

```bash
./bin/omarchy-iso-test release/omarchy.iso --install-only        # once per ISO
./bin/omarchy-iso-test release/omarchy.iso --reuse-base \
  --sync-omarchy ../omarchy                                      # fast loop against local tests
```

Pass `--encrypt` to drive the encrypted install flow (including typing the LUKS passphrase at boot) instead of the unencrypted one. Pass `--no-preview` to collect the same visual artifacts without opening them in `imv` when the run finishes.

## Integration testing the ISO

Scenarios under `test/integration.d/` boot a real ISO install in QEMU and assert on what the running system actually does. The runner installs the ISO once — unattended, from a generated cidata drive — and saves the result as a reusable base image; every scenario then boots a throwaway overlay of that base with its own copy of the firmware vars, so neither disk nor NVRAM state leaks between runs. Shared machinery (VM lifecycle, QMP screendump + OCR console driving, virtual keystrokes, guest SSH, the cidata build) lives in `test/integration.d/base-test.sh`, so a new scenario is one file.

```bash
./test/integration release/omarchy.iso                   # install once, run all scenarios
./test/integration release/omarchy.iso --reuse-base      # fast loop against the saved base
./test/integration release/omarchy.iso factory-reset     # a single named scenario
```

The first scenario is `factory-reset`: it proves `omarchy-system-factory-reset` hands a machine on without destroying a shared ESP. The installed ESP gets a Windows entry with payload plus a second Linux cloned under a foreign machine-id with its own boot directory and UKIs; a real factory reset is then driven through a guest pty, and the harness asserts the foreign entries survive both the staged reset and first-boot provisioning, that the old Omarchy identity is fully retired, and that the machine reaches first-boot setup unattended.

Artifacts — screenshots, the fixtured/staged/final `limine.conf`, the reset typescript, and the factory-reset log — land under `test-runs/<iso>-integration/runs/<timestamp>-<scenario>/`, and `--no-preview` skips the `imv` review just like the acceptance harness.

## Signing release artifacts

Run `./bin/omarchy-iso-sign FILE`. The signing key is retrieved from the shared Omarchy vault with the 1Password CLI. The command can sign either an ISO or its release manifest.

The disabled Apple target has a local, deterministic manifest path so its
release contract can be tested before publication is possible:

```bash
manifest=$(./bin/omarchy-iso-manifest \
  --sequence 26 \
  --version 5.0.0-mac.1 \
  --source-commit "$(git rev-parse HEAD)" \
  --package-snapshot builder/apple-platform-snapshot.json \
  --media-evidence release/omarchy-5.0.0-mac.1.iso.apple-media-evidence.json \
  release/omarchy-5.0.0-mac.1.iso)
./bin/omarchy-iso-sign "$manifest"
./bin/omarchy-iso-manifest-verify \
  --keyring /path/to/omarchy-release-keyring.gpg \
  --state /path/to/trusted/apple-release-state.json \
  --package-snapshot builder/apple-platform-snapshot.json \
  --media-evidence release/omarchy-5.0.0-mac.1.iso.apple-media-evidence.json \
  --commit-state "$manifest" release/omarchy-5.0.0-mac.1.iso
```

The signed canonical JSON binds the exact ISO filename, size, SHA-256, source
commit, independent `aarch64/apple-silicon` target, `asahi-grub` backend, and
content-addressed Apple package snapshot, and the exact static media-evidence
file produced by the build. That evidence proves ISO/ESP `BOOTAA64.EFI`
identity, AArch64 PE format, Apple live-root identity, Asahi initramfs content,
and GRUB paths while explicitly retaining `boot_verified: false` until the
disposable Asahi boot gate runs. Verification rejects unknown or
duplicate fields, noncanonical encodings, changed bytes, lower sequences, and
same-sequence equivocation. The state file is the local anti-rollback trust
anchor: put it in durable storage whose permissions and rollback protection
match the release key's threat model. Verification is read-only unless
`--commit-state` is explicit. This tooling does not enable the Apple build or
publish anything. The current manifest intentionally has `boot_verified: false`;
`omarchy-iso-upload` refuses Apple artifacts until boot verification, the
physical-preview gate, and publication authorization are all represented as
true in a future release-eligible schema.

## Uploading the ISO

Run `./bin/omarchy-iso-upload [release/omarchy.iso]`. This requires rclone configuration (`rclone config`). The `.sig` and `.sha256` sidecars go up with the ISO when they exist beside it. If either `.manifest.json` or `.manifest.json.sig` exists, the uploader requires both plus the media evidence before transferring any file. Apple validation manifests remain unpublishable while any release gate is false.

## Full release of the ISO

Run `./bin/omarchy-iso-release VERSION` to create, test, sign, and upload the ISO in one flow. Add `--rc` to release an RC build instead.

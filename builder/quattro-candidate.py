#!/usr/bin/env python3
"""Verify and freeze signed candidate inputs; never trust artifact-supplied keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

TRUST = Path(__file__).resolve().parent / 'quattro-trust'
NAMES = {'omarchy', 'omarchy-settings', 'omarchy-mac'}
VIDEO_NAMES = {'avd-fw', 'libva-v4l2_request-avd'}
EXTRA_NAMES = {'asdcontrol', 'tobi-try', 'qemu-user-static', 'qemu-user-static-binfmt'}
BOOT_NAMES = {'omarchy-mac-boot', 'limine-mkinitcpio-hook', 'limine-snapper-sync', 'uboot-asahi'}


def package_names(schema):
    require(schema in (1, 2, 3, 4), 'unsupported candidate schema')
    return NAMES | (VIDEO_NAMES if schema >= 2 else set()) | (EXTRA_NAMES if schema >= 3 else set()) | (BOOT_NAMES if schema == 4 else set())


def verify_boot_payloads(payloads):
    owners = {}
    for name, paths in payloads.items():
        for path in paths:
            require(path not in owners, 'candidate payload ownership conflict: ' + path)
            owners[path] = name
    require(owners.get('usr/share/omarchy/default/limine/limine.conf') == 'omarchy-settings',
            'missing ARM64 Limine template')
    require(owners.get('usr/lib/omarchy/initcpio/omarchy-mac-encrypt') == 'omarchy-mac-boot',
            'missing boot conversion payload')
    require(not any('omarchy-arm-repository.key' in p for p in owners), 'upstream repository key is excluded')



def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_file(root, name, destination):
    require(re.fullmatch(r'[A-Za-z0-9+_.:-]+', name) and name not in ('.', '..'), 'unsafe filename')
    fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), 'non-regular input')
        with (destination / name).open('xb') as target:
            shutil.copyfileobj(source, target)


def verify_signature(home, path, policy):
    result = subprocess.run(['gpg', '--homedir', str(home), '--batch', '--no-auto-key-retrieve',
                             '--status-fd', '1', '--verify', str(path) + '.sig', str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    statuses = result.stdout.splitlines()
    valid = [line.split() for line in statuses if line.startswith('[GNUPG:] VALIDSIG ')]
    require(result.returncode == 0 and len(valid) == 1, 'invalid signature: ' + path.name)
    require(valid[0][2] == policy['signing_subkey_fingerprint']
            and valid[0][-1] == policy['primary_fingerprint'], 'unapproved signer')
    require(not any(re.search(r'\b(BADSIG|ERRSIG|REVKEYSIG|EXPKEYSIG|EXPSIG|KEYREVOKED|KEYEXPIRED|SIGEXPIRED)\b', line)
                    for line in statuses), 'expired or revoked signature')


def member(package, name):
    return subprocess.check_output(['bsdtar', '-xOf', str(package), name])


def snapshot(root, destination, receipt_hash, source, trust=TRUST):
    require(re.fullmatch('[a-f0-9]{64}', receipt_hash), 'invalid receipt hash')
    require(re.fullmatch('[a-f0-9]{40}', source), 'invalid source commit')
    require(root.is_dir() and not root.is_symlink(), 'unsafe input directory')
    policy = json.loads((trust / 'policy.json').read_text())
    require(digest(trust / 'public.gpg') == policy['public_key_sha256'], 'trust anchor checksum mismatch')
    destination.mkdir(mode=0o700)  # Exclusive: failed snapshots cannot be reused.
    copy_file(root, 'signing.json', destination)
    copy_file(root, 'signing.json.sig', destination)
    require(digest(destination / 'signing.json') == receipt_hash, 'receipt checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='quattro-public-key-') as temporary:
        home = Path(temporary)
        subprocess.run(['gpg', '--homedir', str(home), '--batch', '--import', str(trust / 'public.gpg')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            verify_signature(home, destination / 'signing.json', policy)
            receipt = json.loads((destination / 'signing.json').read_text())
            require(receipt['schema'] == 1 and receipt['candidate_only'] is True
                    and receipt['publication'] == 'none' and receipt['source_revision'] == source,
                    'wrong candidate receipt')
            require(all(receipt[key] == policy[key] for key in ('primary_fingerprint', 'signing_subkey_fingerprint')),
                    'receipt signer mismatch')
            entries = receipt['files']
            require(len(entries) in (6, 8, 12, 16) and len({p['filename'] for p in entries}) == len(entries), 'unexpected signed inventory')
            for entry in entries:
                name = entry['filename']
                copy_file(root, name, destination)
                copy_file(root, name + '.sig', destination)
                require(digest(destination / name) == entry['sha256'], 'signed file checksum mismatch')
                verify_signature(home, destination / name, policy)
            manifest = destination / 'manifest.json'
            require(digest(manifest) == receipt['input_manifest_sha256'], 'build manifest mismatch')
            data = json.loads(manifest.read_text())
            require(data['schema'] in (1, 2, 3, 4) and data['candidate_only'] is True and data['publication'] == 'none'
                    and data['signing'] == 'none' and data['source_repository'] == 'omacom/omarchy-mac'
                    and data['source_revision'] == source, 'wrong build manifest')
            packages = data['packages']
            names = package_names(data['schema'])
            require(len(packages) == len(names) and {p['name'] for p in packages} == names, 'wrong package set')
            if data['schema'] >= 2:
                require(re.fullmatch('[a-f0-9]{40}', data['package_repository_revision']), 'invalid recipe revision')
            expected = {'manifest.json', 'omarchy-base.packages', 'omarchy-apple.packages'} | {p['filename'] for p in packages}
            require({p['filename'] for p in entries} == expected, 'unexpected signed files')
            versions = {}
            payloads = {}
            for package in packages:
                name, filename = package['name'], package['filename']
                require(re.fullmatch(r'[A-Za-z0-9+_.:-]+\.pkg\.tar\.(xz|zst)', filename), 'invalid archive name')
                path = destination / filename
                require(digest(path) == package['sha256'], 'package checksum mismatch')
                fields = {}
                for line in member(path, '.PKGINFO').decode().splitlines():
                    if ' = ' in line:
                        key, value = line.split(' = ', 1)
                        fields.setdefault(key, []).append(value)
                require(fields.get('pkgname') == [name] and fields.get('arch') == ['any' if name in ('avd-fw', 'tobi-try') else 'aarch64']
                        and fields.get('pkgver') == [package['version']], 'package metadata mismatch')
                require(fields.get('depend', []) == package['dependencies'], 'dependency metadata mismatch')
                revision = 'usr/share/omarchy-mac/source-revision' if name == 'omarchy-mac' else f'usr/share/doc/{name}/source-revision'
                expected_source = data['package_repository_revision'] if name not in NAMES else source
                require(member(path, revision).decode().strip() == expected_source, 'mixed package sources')
                versions[name] = package['version']
                if data['schema'] == 4:
                    paths = subprocess.check_output(['bsdtar', '-tf', str(path)], text=True).splitlines()
                    payloads[name] = {p.removeprefix('./') for p in paths if not p.endswith('/') and not p.removeprefix('./').startswith('.')}

                if name == 'omarchy':
                    for label in ('base', 'apple'):
                        filename = f'omarchy-{label}.packages'
                        require(member(path, 'usr/share/omarchy/install/' + filename) == (destination / filename).read_bytes(),
                                'package manifest differs from archive')
                    require('omarchy-settings=' + package['version'].rsplit('-', 1)[0] in package['dependencies'],
                            'missing settings version pin')
            if data['schema'] == 4:
                verify_boot_payloads(payloads)
            require(versions['omarchy'] == versions['omarchy-settings'], 'mixed desktop versions')
            require('omarchy-mac' in (destination / 'omarchy-apple.packages').read_text().splitlines(), 'missing Apple add-on')
        finally:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'], check=False)
    for path in destination.iterdir():
        path.chmod(0o444)
    destination.chmod(0o555)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt-sha256', required=True)
    parser.add_argument('--source-revision', required=True)
    args = parser.parse_args()
    snapshot(args.input, args.output, args.receipt_sha256, args.source_revision)


if __name__ == '__main__':
    main()

#!/usr/bin/python3
"""Freeze an authenticated dependency snapshot using the pinned candidate key."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location('candidate', Path(__file__).with_name('quattro-candidate.py'))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
EXCLUDED = c.NAMES | c.VIDEO_NAMES


def snapshot(root, destination, manifest_hash, trust=c.TRUST):
    c.require(re.fullmatch('[a-f0-9]{64}', manifest_hash), 'invalid dependency manifest hash')
    c.require(root.is_dir() and not root.is_symlink(), 'unsafe dependency root')
    policy = json.loads((trust / 'policy.json').read_text())
    c.require(c.digest(trust / 'public.gpg') == policy['public_key_sha256'], 'trust anchor checksum mismatch')
    destination.mkdir(mode=0o700)
    for name in ('manifest.json', 'manifest.json.sig'):
        c.copy_file(root, name, destination)
    c.require(c.digest(destination / 'manifest.json') == manifest_hash, 'dependency manifest checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='dependency-public-key-') as tmp:
        home = Path(tmp)
        subprocess.run(['gpg', '--homedir', str(home), '--batch', '--import', str(trust / 'public.gpg')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            c.verify_signature(home, destination / 'manifest.json', policy)
            data = json.loads((destination / 'manifest.json').read_text())
            c.require(data['schema'] == 1 and data['kind'] == 'omarchy-image-dependencies'
                      and data['publication'] == 'none' and data['source_repository'] == 'omarchy-mac/omarchy-pkgs-aarch64'
                      and data['source_lane'] == 'edge' and data['excluded_names'] == sorted(EXCLUDED), 'wrong dependency contract')
            records = data['packages']
            names = [r['name'] for r in records]
            c.require(len(names) == len(set(names)) and 'omarchy-nvim' in names and not EXCLUDED.intersection(names),
                      'wrong dependency package set')
            files = [r['filename'] for r in records]
            c.require(len(files) == len(set(files)), 'duplicate archive filename')
            expected = {'manifest.json', 'origin.db', *files}
            c.require({p.name for p in root.iterdir()} == expected | {n + '.sig' for n in expected}, 'dependency file inventory differs')
            for name in ['origin.db', *files]:
                c.copy_file(root, name, destination)
                c.copy_file(root, name + '.sig', destination)
                c.verify_signature(home, destination / name, policy)
            c.require(c.digest(destination / 'origin.db') == data['source_database_sha256'], 'origin database checksum mismatch')
            # The signed origin binds the complete published inventory. Never
            # accept an omitted dependency or substitution from the candidate set.
            origin = {}
            db = destination / 'origin.db'
            for member in subprocess.check_output(['bsdtar', '-tf', str(db)], text=True).splitlines():
                if not member.endswith('/desc'):
                    continue
                fields = {}; key = None
                for line in c.member(db, member).decode().splitlines():
                    if line.startswith('%') and line.endswith('%'):
                        key = line; fields[key] = []
                    elif line and key:
                        fields[key].append(line)
                name = fields['%NAME%'][0]
                c.require(name not in origin, 'duplicate origin package')
                origin[name] = fields
            c.require(set(names) == set(origin) - EXCLUDED, 'incomplete origin inventory')
            for record in records:
                filename = record['filename']
                c.require(re.fullmatch(r'[A-Za-z0-9+_.:-]+\.pkg\.tar\.(xz|zst)', filename), 'unsafe archive filename')
                path = destination / filename
                c.require(c.digest(path) == record['sha256'], 'dependency archive checksum mismatch')
                fields = {}
                for line in c.member(path, '.PKGINFO').decode().splitlines():
                    if ' = ' in line:
                        key, value = line.split(' = ', 1)
                        fields.setdefault(key, []).append(value)
                c.require(record['arch'] in ('any', 'aarch64') and fields.get('arch') == [record['arch']]
                          and fields.get('pkgname') == [record['name']] and fields.get('pkgver') == [record['version']]
                          and fields.get('depend', []) == record['depends'], 'dependency metadata mismatch')
                row = origin[record['name']]
                for field, value in [('FILENAME', filename), ('VERSION', record['version']), ('SHA256SUM', record['sha256'])]:
                    c.require(row['%' + field + '%'] == [value], 'dependency differs from origin')
        finally:
            subprocess.run(['gpgconf', '--homedir', str(home), '--kill', 'gpg-agent'], check=False)
    for path in destination.iterdir():
        path.chmod(0o444)
    destination.chmod(0o555)
    return data


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--manifest-sha256', required=True)
    a = p.parse_args()
    snapshot(a.input, a.output, a.manifest_sha256)

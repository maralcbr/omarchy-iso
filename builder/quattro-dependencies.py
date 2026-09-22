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
LIMINE_EXCLUDED = c.package_names(4) | {'omarchy-apple-boot', 'omarchy-first-boot'}


def dependency_contract(data, candidate_schema):
    c.package_names(candidate_schema)
    if candidate_schema == 4:
        c.require(data['schema'] == 2 and data.get('boot_profile') == 'limine'
                  and data.get('candidate_schema') == 4, 'wrong Limine dependency contract')
        excluded = LIMINE_EXCLUDED
    else:
        c.require(data['schema'] == 1 and 'boot_profile' not in data
                  and 'candidate_schema' not in data, 'wrong legacy dependency contract')
        excluded = EXCLUDED
    c.require(data['kind'] == 'omarchy-image-dependencies' and data['publication'] == 'none'
              and data['source_repository'] == 'omarchy-mac/omarchy-pkgs-aarch64'
              and data['source_lane'] == 'edge' and data['excluded_names'] == sorted(excluded),
              'wrong dependency contract')
    return excluded


def platform_selection(dependencies, candidate, platform, overlays=()):
    """Select the pinned platform without hiding an accidental repository overlap."""
    excluded = dependency_contract(dependencies, candidate['schema'])
    for data in (dependencies, candidate, platform):
        c.require(len({p['name'] for p in data['packages']}) == len(data['packages']), 'duplicate package name')
    names = [{p['name'] for p in data['packages']} for data in (dependencies, candidate, platform)]
    dependency_names, candidate_names, platform_names = names
    c.require(candidate_names == c.package_names(candidate['schema']), 'wrong candidate package set')
    c.require(not dependency_names & excluded, 'excluded dependency package')
    c.require(not dependency_names & candidate_names, 'candidate and dependency packages overlap')
    replacement = {'uboot-asahi'} if candidate['schema'] == 4 else set()
    c.require(candidate_names & platform_names == replacement, 'candidate and platform packages overlap')
    selected = platform_names - replacement
    if 'trust' in platform:
        selected.add('asahi-alarm-keyring')
    c.require(not dependency_names & selected, 'dependency and platform packages overlap')
    overlay_names = {p['name'] for p in overlays}
    c.require(len(overlay_names) == len(overlays), 'duplicate platform overlay')
    c.require(not overlay_names & (dependency_names | candidate_names | selected), 'platform overlay packages overlap')
    return [p['filename'] for p in platform['packages'] if p['name'] in selected]



def snapshot(root, destination, manifest_hash, trust=c.TRUST, *, candidate_schema=3):
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
            excluded = dependency_contract(data, candidate_schema)
            records = data['packages']
            names = [r['name'] for r in records]
            c.require(len(names) == len(set(names)) and 'omarchy-nvim' in names and not excluded.intersection(names),
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
            c.require(set(names) == set(origin) - excluded, 'incomplete origin inventory')
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
    p.add_argument('--candidate-schema', type=int, choices=(1, 2, 3, 4), default=3)
    p.add_argument('--candidate-manifest', type=Path)
    p.add_argument('--platform-manifest', type=Path)
    p.add_argument('--platform-overlay', type=Path, action='append', default=[])
    p.add_argument('--selected-platform', type=Path)
    a = p.parse_args()
    data = snapshot(a.input, a.output, a.manifest_sha256, candidate_schema=a.candidate_schema)
    if any((a.candidate_manifest, a.platform_manifest, a.selected_platform, a.platform_overlay)):
        c.require(all((a.candidate_manifest, a.platform_manifest, a.selected_platform)), 'incomplete platform selection arguments')
        candidate = json.loads(a.candidate_manifest.read_text())
        c.require(candidate['schema'] == a.candidate_schema, 'candidate schema differs')
        files = platform_selection(data, candidate, json.loads(a.platform_manifest.read_text()),
                                   [json.loads(path.read_text()) for path in a.platform_overlay])
        a.selected_platform.write_text(''.join(name + '\n' for name in files))


#!/usr/bin/env python3
"""Offline signed-input fixtures; never use production secrets or host trust."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('candidate', ROOT / 'builder/quattro-candidate.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CandidateTest(unittest.TestCase):
    schema = 1
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.home = cls.base / 'gpg'
        cls.home.mkdir(mode=0o700)
        cls.gpg = ['gpg', '--homedir', str(cls.home), '--batch', '--pinentry-mode', 'loopback', '--passphrase', '']
        def gpg(*args):
            return subprocess.check_output([*cls.gpg, *args], stderr=subprocess.DEVNULL)
        gpg('--quick-generate-key', 'Candidate test <fixture@example.invalid>', 'ed25519', 'cert', '1d')
        cls.primary = gpg('--with-colons', '--list-keys').decode().split('fpr:::::::::')[1].split(':')[0]
        gpg('--quick-add-key', cls.primary, 'ed25519', 'sign', '1d')
        cls.subkey = gpg('--with-colons', '--list-keys').decode().split('fpr:::::::::')[2].split(':')[0]
        cls.trust = cls.base / 'trust'
        cls.trust.mkdir()
        (cls.trust / 'public.gpg').write_bytes(gpg('--export', cls.primary))
        cls.policy = dict(primary_fingerprint=cls.primary, signing_subkey_fingerprint=cls.subkey,
                          public_key_sha256=c.digest(cls.trust / 'public.gpg'))
        (cls.trust / 'policy.json').write_text(json.dumps(cls.policy))
        cls.source = 'a' * 40
        cls.bundle = cls.base / 'bundle'
        cls.bundle.mkdir()
        packages = []
        for name in sorted(c.package_names(cls.schema)):
            deps = ['omarchy-settings=1.0'] if name == 'omarchy' else []
            content = {'.PKGINFO': '\n'.join([f'pkgname = {name}', 'pkgver = 1.0-1', 'arch = any' if name in ('avd-fw', 'tobi-try') else 'arch = aarch64', *['depend = ' + d for d in deps]])}
            revision = 'usr/share/omarchy-mac/source-revision' if name == 'omarchy-mac' else f'usr/share/doc/{name}/source-revision'
            content[revision] = ('b' * 40 if name not in c.NAMES else cls.source) + '\n'
            if name == 'omarchy':
                for label in ('base', 'apple'):
                    filename = f'omarchy-{label}.packages'
                    (cls.bundle / filename).write_text('omarchy-mac\n')
                    content['usr/share/omarchy/install/' + filename] = 'omarchy-mac\n'
            if cls.schema == 4 and name == 'omarchy-settings':
                content['usr/share/omarchy/default/limine/limine.conf'] = 'fixture menu'
            if cls.schema == 4 and name == 'omarchy-mac-boot':
                content['usr/lib/omarchy/initcpio/omarchy-mac-encrypt'] = 'fixture converter'
            filename = name + '-1.0-1-aarch64.pkg.tar.xz'
            with tarfile.open(cls.bundle / filename, 'w:xz') as archive:
                for path, value in content.items():
                    raw = value.encode()
                    member = tarfile.TarInfo(path)
                    member.size = len(raw)
                    archive.addfile(member, io.BytesIO(raw))
            packages.append(dict(name=name, version='1.0-1', filename=filename, dependencies=deps,
                                 sha256=c.digest(cls.bundle / filename)))
        manifest = dict(schema=cls.schema, package_repository_revision='b' * 40, candidate_only=True, publication='none', signing='none',
                        source_repository='omacom/omarchy-mac', source_revision=cls.source, packages=packages)
        (cls.bundle / 'manifest.json').write_text(json.dumps(manifest))
        receipt = dict(schema=1, candidate_only=True, publication='none', source_revision=cls.source,
                       primary_fingerprint=cls.primary, signing_subkey_fingerprint=cls.subkey,
                       input_manifest_sha256=c.digest(cls.bundle / 'manifest.json'),
                       files=[dict(filename=p.name, sha256=c.digest(p)) for p in sorted(cls.bundle.iterdir())])
        (cls.bundle / 'signing.json').write_text(json.dumps(receipt))
        for path in list(cls.bundle.iterdir()):
            gpg('--local-user', cls.subkey + '!', '--detach-sign', str(path))
        cls.receipt_hash = c.digest(cls.bundle / 'signing.json')

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['gpgconf', '--homedir', str(cls.home), '--kill', 'gpg-agent'], check=True)
        for path in cls.base.rglob('*'):
            if path.is_dir(): path.chmod(0o700)
        cls.tmp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.base))
        self.input = self.work / 'input'
        shutil.copytree(self.bundle, self.input)

    def verify(self, **kwargs):
        return c.snapshot(self.input, self.work / 'output', kwargs.get('checksum', self.receipt_hash),
                          kwargs.get('source', self.source), kwargs.get('trust', self.trust))

    def test_signed_set_creates_readonly_snapshot(self):
        self.assertEqual(len(self.verify()['packages']), len(c.package_names(self.schema)))
        self.assertEqual((self.work / 'output').stat().st_mode & 0o777, 0o555)

    def test_tampered_archive(self):
        next(self.input.glob('*.pkg.tar.xz')).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.verify()

    def test_unsigned_receipt_checksum_source_and_symlink(self):
        for change in ('signature', 'checksum', 'source', 'symlink'):
            with self.subTest(change=change):
                work = self.work / change
                shutil.copytree(self.bundle, work)
                if change == 'signature': (work / 'signing.json.sig').unlink()
                if change == 'symlink':
                    (work / 'manifest.json').unlink()
                    (work / 'manifest.json').symlink_to(self.bundle / 'manifest.json')
                with self.assertRaises((ValueError, OSError)):
                    c.snapshot(work, self.work / (change + '-out'),
                               '0' * 64 if change == 'checksum' else self.receipt_hash,
                               'b' * 40 if change == 'source' else self.source, self.trust)

    def test_artifact_cannot_supply_trust_anchor(self):
        trust = self.work / 'trust'
        shutil.copytree(self.trust, trust)
        (trust / 'public.gpg').write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError, 'trust anchor'):
            self.verify(trust=trust)

    def test_unsigned_checksum_file_is_ignored(self):
        (self.input / 'SHA256SUMS').write_text('not an authentication input\n')
        self.verify()


class VideoCandidateTest(CandidateTest):
    schema = 2

    def resign_manifest(self, data):
        manifest = self.input / 'manifest.json'
        manifest.write_text(json.dumps(data))
        receipt_path = self.input / 'signing.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['input_manifest_sha256'] = c.digest(manifest)
        for entry in receipt['files']:
            if entry['filename'] == 'manifest.json':
                entry['sha256'] = c.digest(manifest)
        receipt_path.write_text(json.dumps(receipt))
        for path in (manifest, receipt_path):
            Path(str(path) + '.sig').unlink()
            subprocess.run([*self.gpg, '--local-user', self.subkey + '!', '--detach-sign', str(path)], check=True)
        return c.digest(receipt_path)

    def test_wrong_video_recipe_revision(self):
        data = json.loads((self.input / 'manifest.json').read_text())
        data['package_repository_revision'] = 'c' * 40
        checksum = self.resign_manifest(data)
        with self.assertRaisesRegex(ValueError, 'mixed package sources'):
            self.verify(checksum=checksum)

    def test_missing_video_package(self):
        data = json.loads((self.input / 'manifest.json').read_text())
        data['packages'] = [p for p in data['packages'] if p['name'] != 'avd-fw']
        checksum = self.resign_manifest(data)
        with self.assertRaisesRegex(ValueError, 'wrong package set'):
            self.verify(checksum=checksum)


class CompleteCandidateTest(VideoCandidateTest):
    schema = 3

    def test_missing_static_binfmt_package(self):
        data = json.loads((self.input / 'manifest.json').read_text())
        data['packages'] = [p for p in data['packages'] if p['name'] != 'qemu-user-static-binfmt']
        checksum = self.resign_manifest(data)
        with self.assertRaisesRegex(ValueError, 'wrong package set'):
            self.verify(checksum=checksum)


class BootCandidateTest(CompleteCandidateTest):
    schema = 4

    def test_image_assembly_stops_before_trust_changes(self):
        script = ROOT / 'builder/quattro-candidate-packages.sh'
        result = subprocess.run(['bash', '-c', r'''source "$1"
python3() { return 0; }
jq() { printf '4\n'; }
pacman-key() { echo TRUST-CHANGED; return 99; }
export OMARCHY_CANDIDATE_ROOT=fixture OMARCHY_CANDIDATE_RECEIPT_SHA256=fixture OMARCHY_CANDIDATE_SOURCE=fixture
export OMARCHY_BUILD_MODE=diagnostic OMARCHY_MEDIA_TARGET=aarch64/apple-silicon OMARCHY_ARTIFACT_KIND=asahi-os-package
preflight_quattro_candidate_packages
''', 'fixture', str(script)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('image assembly is not enabled', result.stderr)
        self.assertNotIn('TRUST-CHANGED', result.stdout)

    def test_full_cache_initializer_refuses_before_all_trust_and_downloads(self):
        stage = (ROOT / 'builder/asahi-stages/verified-package-cache.sh').read_text()
        stage = stage.replace('source /builder/', 'source "' + str(ROOT / 'builder') + '/')
        stage = '\n'.join(line + '"' if 'source "' in line else line for line in stage.splitlines())
        result = subprocess.run(['bash', '-c', stage + r"""
python3() { return 0; }
jq() { printf '4\n'; }
pacman-key() { echo TRUST-CHANGED; return 99; }
prepare_verified_package_snapshots_and_trust() { echo DEPENDENCIES-STARTED; return 99; }
mkdir() { echo CACHE-TOUCHED; return 99; }
export OMARCHY_CANDIDATE_ROOT=fixture OMARCHY_CANDIDATE_RECEIPT_SHA256=fixture OMARCHY_CANDIDATE_SOURCE=fixture
export OMARCHY_BUILD_MODE=diagnostic OMARCHY_MEDIA_TARGET=aarch64/apple-silicon OMARCHY_ARTIFACT_KIND=asahi-os-package
initialize_verified_package_cache_stage
"""], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('image assembly is not enabled', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_missing_boot_package(self):
        data = json.loads((self.input / 'manifest.json').read_text())
        data['packages'] = [p for p in data['packages'] if p['name'] != 'omarchy-mac-boot']
        checksum = self.resign_manifest(data)
        with self.assertRaisesRegex(ValueError, 'wrong package set'):
            self.verify(checksum=checksum)

    def test_boot_ownership_and_foreign_key_rejected(self):
        paths = {'omarchy-settings': {'usr/share/omarchy/default/limine/limine.conf'},
                 'omarchy-mac-boot': {'usr/lib/omarchy/initcpio/omarchy-mac-encrypt'}}
        c.verify_boot_payloads(paths)
        for owner, path, error in (
            ('omarchy', 'usr/share/omarchy/default/limine/limine.conf', 'ownership conflict'),
            ('omarchy-mac-boot', 'usr/lib/omarchy/mac-first-boot/omarchy-arm-repository.key', 'repository key'),
        ):
            changed = {name: set(files) for name, files in paths.items()}
            changed.setdefault(owner, set()).add(path)
            with self.assertRaisesRegex(ValueError, error):
                c.verify_boot_payloads(changed)
        with self.assertRaisesRegex(ValueError, 'Limine template'):
            c.verify_boot_payloads({'omarchy-mac-boot': paths['omarchy-mac-boot']})


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/python3
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import test_quattro_candidate as fixture

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('dependencies', ROOT/'builder/quattro-dependencies.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DependencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.CandidateTest.setUpClass()
        cls.keys = fixture.CandidateTest

    @classmethod
    def tearDownClass(cls):
        fixture.CandidateTest.tearDownClass()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=self.keys.base)
        self.root = Path(self.tmp.name)
        self.input = self.root/'input'; self.input.mkdir()
        records = []
        for name in ('omarchy-nvim', 'omarchy-steam-fex'):
            filename = name+'-1-1-any.pkg.tar.xz'
            self.tar(self.input/filename, {'.PKGINFO':f'pkgname = {name}\npkgver = 1-1\narch = any\n'})
            records.append(dict(name=name, version='1-1', arch='any', filename=filename,
                                sha256=d.c.digest(self.input/filename), depends=[]))
        origin = {}
        for r in records:
            origin[r['name']+'/desc'] = ''.join('%'+key+'%\n'+r[value]+'\n\n' for key,value in
                [('NAME','name'),('VERSION','version'),('FILENAME','filename'),('SHA256SUM','sha256')])
        self.tar(self.input/'origin.db', origin)
        self.data = dict(schema=1, kind='omarchy-image-dependencies', publication='none',
            source_repository='omarchy-mac/omarchy-pkgs-aarch64', source_lane='edge',
            source_database_sha256=d.c.digest(self.input/'origin.db'), excluded_names=sorted(d.EXCLUDED),packages=records)
        self.sign_manifest()
        for p in list(self.input.iterdir()):
            if not p.name.endswith('.sig') and p.name != 'manifest.json':self.sign(p)

    def tearDown(self):
        for p in self.root.rglob('*'):
            if p.is_dir():p.chmod(0o700)
        self.tmp.cleanup()

    def tar(self, path, files):
        with tarfile.open(path,'w:xz') as tar:
            for name,value in files.items():
                data=value.encode(); member=tarfile.TarInfo(name);member.size=len(data)
                tar.addfile(member,io.BytesIO(data))

    def sign(self,path):
        Path(str(path)+'.sig').unlink(missing_ok=True)
        subprocess.run([*self.keys.gpg,'--local-user',self.keys.subkey+'!','--detach-sign',str(path)],check=True,stderr=subprocess.DEVNULL)

    def sign_manifest(self):
        p=self.input/'manifest.json';p.write_text(json.dumps(self.data));self.sign(p)
        self.checksum=d.c.digest(p)

    def verify(self):
        return d.snapshot(self.input,self.root/'output',self.checksum,self.keys.trust)

    def test_authenticated_snapshot_is_immutable(self):
        self.assertEqual(self.verify(),self.data)
        self.assertEqual((self.root/'output').stat().st_mode&0o777,0o555)

    def test_bad_signature_and_symlink_rejected(self):
        p=self.input/self.data['packages'][0]['filename'];p.unlink();p.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):self.verify()

    def test_signed_omission_rejected_against_origin(self):
        r=self.data['packages'].pop()
        (self.input/r['filename']).unlink();(self.input/(r['filename']+'.sig')).unlink()
        self.sign_manifest()
        with self.assertRaisesRegex(ValueError,'incomplete origin'):self.verify()

    def test_candidate_substitution_rejected(self):
        self.data['packages'][1]['name']='omarchy';self.sign_manifest()
        with self.assertRaisesRegex(ValueError,'wrong dependency package set'):self.verify()

    def test_tampered_archive_rejected(self):
        (self.input/self.data['packages'][0]['filename']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'invalid signature'):self.verify()

    def test_wrong_manifest_and_extra_file_rejected(self):
        self.checksum='0'*64
        with self.assertRaisesRegex(ValueError,'checksum'):self.verify()
        shutil.rmtree(self.root/'output')
        self.checksum=d.c.digest(self.input/'manifest.json')
        (self.input/'extra').write_text('unselected')
        with self.assertRaisesRegex(ValueError,'file inventory'):self.verify()


if __name__=='__main__':unittest.main()

"""The private image lane admits only its measured signed candidate and product."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'builder' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


admission = module('admission', 'private-limine-qualification.py')
package = module('package', 'verify-asahi-os-package.py')


class PrivateLimine(unittest.TestCase):
    def setUp(self):
        self.product = json.loads((ROOT / 'builder/products/omarchy-mx-mac-limine-private.json').read_text())
        private = self.product['private_qualification']
        self.candidate = {'schema': 4, 'source_revision': private['source_revision'], 'packages': [
            {'name': 'uboot-asahi', 'filename': private['uboot_filename'], 'sha256': private['uboot_sha256']}]}
        self.dependencies = {'schema': 2, 'candidate_schema': 4, 'boot_profile': 'limine'}
        self.policy = hashlib.sha256((ROOT / 'builder/quattro-trust/policy.json').read_bytes()).hexdigest()

    def test_only_measured_candidate_and_private_signer_are_admitted(self):
        admission.admit(self.product, self.candidate, self.dependencies, self.policy)
        mutations = [
            ('product', 'boot_backend', 'asahi-grub'),
            ('candidate', 'schema', 3),
            ('candidate', 'source_revision', '0' * 40),
            ('dependencies', 'schema', 1),
            ('dependencies', 'candidate_schema', 3),
            ('dependencies', 'boot_profile', 'grub'),
        ]
        for obj, key, value in mutations:
            with self.subTest(obj=obj, key=key):
                product, candidate, dependencies = copy.deepcopy((self.product, self.candidate, self.dependencies))
                {'product': product, 'candidate': candidate, 'dependencies': dependencies}[obj][key] = value
                with self.assertRaises(ValueError):
                    admission.admit(product, candidate, dependencies, self.policy)
        with self.assertRaises(ValueError):
            admission.admit(self.product, self.candidate, self.dependencies, '0' * 64)
        for key in ('filename', 'sha256'):
            candidate = copy.deepcopy(self.candidate)
            candidate['packages'][0][key] += 'changed'
            with self.assertRaisesRegex(ValueError, 'U-Boot'):
                admission.admit(self.product, candidate, self.dependencies, self.policy)

    def test_default_product_and_grub_branding_remain_unchanged(self):
        baseline = package.load_product(ROOT / 'builder/products/omarchy-mx-mac.json')
        self.assertEqual(baseline['boot_backend'], 'asahi-grub')
        self.assertNotIn('private_qualification', baseline)
        self.assertEqual(baseline['branding']['m1n1_boot_sha256'], '60e1c808b7a0c70f294c7cdfa34a5ed4de0d19df08700a49851055e7ea865bd8')
        manifest = json.loads((ROOT / 'builder/branding/branding-manifest-limine-private.json').read_text())
        self.assertEqual(manifest['m1n1']['output']['sha256'], self.product['branding']['m1n1_boot_sha256'])

    def test_private_product_requires_diagnostic_signed_inputs_before_cache_access(self):
        result = subprocess.run(['bash', str(ROOT / 'bin/omarchy-iso-make'), '--product',
            'omarchy-mx-mac-limine-private', '--mode', 'diagnostic', '--target',
            'aarch64/apple-silicon', '--artifact', 'asahi-os-package'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Private Limine qualification requires', result.stderr)

    def test_zip_contract_rejects_wrong_loader_missing_boot_artifacts_and_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = copy.deepcopy(self.product)
            product.update(package_filename='private.zip', boot_size_bytes=4096, root_size_bytes=131072)
            boot = bytearray(4096); boot[0x438:0x43a] = b'\x53\xef'
            system = bytearray(131072); system[0x10040:0x10048] = b'_BHRfS_M'
            efi = bytearray(512); efi[:2] = b'MZ'; struct.pack_into('<I', efi, 60, 128)
            efi[128:132] = b'PE\0\0'; struct.pack_into('<H', efi, 132, 0xaa64)
            product['private_qualification']['limine_efi_sha256'] = hashlib.sha256(efi).hexdigest()
            product['branding'].update(m1n1_boot_sha256=hashlib.sha256(b'm1n1').hexdigest(),
                volume_icon_size_bytes=4, volume_icon_sha256=hashlib.sha256(b'icon').hexdigest())
            descriptor = root / 'product.json'; descriptor.write_text(json.dumps(product))
            files = {'boot.img': boot, 'root.img': system, 'esp/EFI/BOOT/BOOTAA64.EFI': efi,
                'esp/m1n1/boot.bin': b'm1n1', 'omarchy-volume.icns': b'icon',
                'esp/limine.conf': b'menu', 'esp/EFI/Linux/omarchy_linux-asahi.efi': b'uki'}
            def write(files):
                target = root / 'private.zip'
                with zipfile.ZipFile(target, 'w') as archive:
                    for name, value in files.items(): archive.writestr(name, value)
                return target
            self.assertEqual(package.verify(write(files), descriptor)['checks']['boot_backend'], 'asahi-limine')
            changed = dict(files); changed['esp/EFI/BOOT/BOOTAA64.EFI'] = efi + b'changed'
            with self.assertRaisesRegex(package.PackageVerificationError, 'EFI digest'):
                package.verify(write(changed), descriptor)
            for missing in ('esp/limine.conf', 'esp/EFI/Linux/omarchy_linux-asahi.efi'):
                with self.assertRaisesRegex(package.PackageVerificationError, 'menu or UKI'):
                    package.verify(write({k: v for k, v in files.items() if k != missing}), descriptor)
            with self.assertRaisesRegex(package.PackageVerificationError, 'staging'):
                package.verify(write(files | {'esp/omarchy/secret': b'leftover'}), descriptor)


if __name__ == '__main__':
    unittest.main()

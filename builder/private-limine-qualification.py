#!/usr/bin/python3
"""Admit only the measured private Limine candidate after signature verification."""
import hashlib
import json
from pathlib import Path
import sys


def admit(product, candidate, dependencies, policy_digest):
    private = product.get('private_qualification', {})
    if (product.get('boot_backend') != 'asahi-limine'
            or product.get('kernel_package') != 'linux-asahi'
            or private.get('publication') != 'none'
            or private.get('candidate_schema') != 4
            or private.get('dependency_schema') != 2
            or candidate.get('schema') != 4
            or candidate.get('source_revision') != private.get('source_revision')
            or dependencies.get('schema') != 2
            or dependencies.get('candidate_schema') != 4
            or dependencies.get('boot_profile') != 'limine'
            or policy_digest != private.get('signing_policy_sha256')):
        raise ValueError('private Limine candidate, dependency or signer contract differs')
    packages = [p for p in candidate['packages'] if p['name'] == 'uboot-asahi']
    if len(packages) != 1 or any(packages[0][key] != private['uboot_' + key]
                                for key in ('filename', 'sha256')):
        raise ValueError('candidate U-Boot differs from measured private branding')


if __name__ == '__main__':
    try:
        product, candidate, dependencies = [json.loads(Path(p).read_text()) for p in sys.argv[1:4]]
        digest = hashlib.sha256(Path(sys.argv[4]).read_bytes()).hexdigest()
        admit(product, candidate, dependencies, digest)
    except (OSError, ValueError, KeyError, IndexError) as error:
        sys.exit(f'private-limine-qualification: {error}')

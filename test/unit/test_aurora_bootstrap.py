"""Exercise the adapter's first-transaction provider selection without archinstall."""
import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / 'configs/airootfs/usr/share/omarchy-iso/orchestrator/archinstall_adapter.py'


class AuroraBootstrapTest(unittest.TestCase):
    def test_initial_transaction_selects_only_the_product_bootloader(self):
        tree = ast.parse(ADAPTER.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'open_installer')
        function.returns = None
        for arg in function.args.args:
            arg.annotation = None
        module = ast.Module(body=[function], type_ignores=[])
        for kernel, expected in (
            ('linux-aurora', ['base', 'sudo', 'linux-firmware', 'mkinitcpio', 'm1n1-aurora']),
            ('linux-asahi', None),
            ('linux', None),
        ):
            with self.subTest(kernel=kernel):
                constructor = MagicMock()
                namespace = {'contextmanager': contextmanager, 'Installer': constructor}
                exec(compile(module, str(ADAPTER), 'exec'), namespace)
                config = SimpleNamespace(disk_config=object(), kernels=[kernel])
                with namespace['open_installer'](config, Path('/target')):
                    pass
                constructor.assert_called_once_with(
                    Path('/target'), config.disk_config, base_packages=expected,
                    kernels=[kernel], silent=True,
                )


if __name__ == '__main__':
    unittest.main()

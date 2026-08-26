import sys
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/local/lib"))
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))

from omarchy.apple_handoff import (  # noqa: E402
    DiskLayout,
    Extent,
    HandoffError,
    Partition,
    VerifiedHandoff,
    EFI_SYSTEM_PARTITION_TYPE,
    LINUX_FILESYSTEM_PARTITION_TYPE,
)
from orchestrator.apple_backend import (  # noqa: E402
    APPLE_BOOT_BACKEND,
    APPLE_PLATFORM,
    build_apple_storage_plan,
)
from orchestrator.apple_storage_executor import (  # noqa: E402
    AppleStorageExecutor,
    KernelDeviceIdentity,
    validate_kernel_identities,
    install_authorized_grub,
)


class AppleBackendTests(unittest.TestCase):
    def setUp(self):
        self.target = Partition(
            node="/dev/nvme0n1p5",
            partition_uuid="77777777-6666-4555-8444-333333333333",
            partition_type=LINUX_FILESYSTEM_PARTITION_TYPE,
            extent=Extent(700_000_000_000, 100_000_000_000),
        )
        self.esp = Partition(
            node="/dev/nvme0n1p4",
            partition_uuid="11111111-2222-4333-8444-555555555555",
            partition_type=EFI_SYSTEM_PARTITION_TYPE,
            extent=Extent(699_000_000_000, 500_170_752),
        )
        self.handoff = VerifiedHandoff(
            installation_id="12345678-1234-4234-9234-123456789abc",
            sequence=7,
            device_identifier="apple,j314s",
            plan_digest="a" * 64,
            disk=DiskLayout(
                device="/dev/nvme0n1",
                gpt_disk_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                size_bytes=1_000_000_000_000,
                logical_sector_bytes=512,
                partitions=(self.esp, self.target),
            ),
            install_partition=self.target,
            paired_esp=self.esp,
        )

    def test_plan_formats_only_the_exact_asahi_created_partition(self):
        plan = build_apple_storage_plan(self.handoff)

        self.assertEqual(plan.platform, APPLE_PLATFORM)
        self.assertEqual(plan.boot_backend, APPLE_BOOT_BACKEND)
        self.assertFalse(plan.execution_ready)
        self.assertIn("not wired", plan.execution_blocker)
        self.assertEqual(plan.format_command[-1], self.target.node)
        self.assertEqual(plan.mount_command[-2:], (self.target.node, "/mnt"))
        self.assertNotIn(self.handoff.disk.device, plan.format_command)
        self.assertNotIn(self.esp.node, plan.format_command)

    def test_plan_never_claims_boot_is_ready(self):
        plan = build_apple_storage_plan(self.handoff)

        self.assertFalse(plan.boot_ready)
        self.assertIn("not wired", plan.boot_blocker)
        self.assertEqual(
            plan.paired_esp_mount_options,
            ("ro", "nosuid", "nodev", "noexec"),
        )

    def test_target_relative_writes_are_bounded_by_signed_extent(self):
        plan = build_apple_storage_plan(self.handoff)

        plan.assert_target_write(0, 4096)
        plan.assert_target_write(self.target.extent.length_bytes - 4096, 4096)
        with self.assertRaisesRegex(HandoffError, "escapes"):
            plan.assert_target_write(self.target.extent.length_bytes - 1, 4096)
        with self.assertRaisesRegex(HandoffError, "negative"):
            plan.assert_target_write(-1, 4096)

    def test_whole_disk_cannot_be_substituted_for_target_partition(self):
        unsafe = VerifiedHandoff(
            **{
                **self.handoff.__dict__,
                "install_partition": Partition(
                    node=self.handoff.disk.device,
                    partition_uuid=self.target.partition_uuid,
                    partition_type=self.target.partition_type,
                    extent=self.target.extent,
                ),
            }
        )

        with self.assertRaisesRegex(HandoffError, "must be distinct"):
            build_apple_storage_plan(unsafe)

    def test_paired_esp_cannot_be_substituted_for_target_partition(self):
        unsafe = VerifiedHandoff(
            **{
                **self.handoff.__dict__,
                "install_partition": Partition(
                    node=self.esp.node,
                    partition_uuid=self.target.partition_uuid,
                    partition_type=self.target.partition_type,
                    extent=self.target.extent,
                ),
            }
        )

        with self.assertRaisesRegex(HandoffError, "must be distinct"):
            build_apple_storage_plan(unsafe)

    def test_non_normalized_device_path_fails_closed(self):
        unsafe = VerifiedHandoff(
            **{
                **self.handoff.__dict__,
                "install_partition": Partition(
                    node="/dev//nvme0n1p5",
                    partition_uuid=self.target.partition_uuid,
                    partition_type=self.target.partition_type,
                    extent=self.target.extent,
                ),
            }
        )

        with self.assertRaisesRegex(HandoffError, "absolute /dev paths"):
            build_apple_storage_plan(unsafe)

    def test_kernel_identity_gate_matches_signed_disk_and_partition_extents(self):
        plan = build_apple_storage_plan(self.handoff)
        disk_path = Path("/sys/devices/platform/arm-io/block/nvme0n1")
        validate_kernel_identities(
            plan,
            KernelDeviceIdentity(1, disk_path, 1_953_125_000, None, 512),
            KernelDeviceIdentity(2, disk_path / "nvme0n1p5", 195_312_500, 1_367_187_500, 512),
            KernelDeviceIdentity(3, disk_path / "nvme0n1p4", 976_896, 1_365_234_375, 512),
        )

    def test_kernel_identity_gate_rejects_target_extent_substitution(self):
        plan = build_apple_storage_plan(self.handoff)
        disk_path = Path("/sys/devices/platform/arm-io/block/nvme0n1")
        with self.assertRaisesRegex(HandoffError, "target partition offset changed"):
            validate_kernel_identities(
                plan,
                KernelDeviceIdentity(1, disk_path, 1_953_125_000, None, 512),
                KernelDeviceIdentity(2, disk_path / "nvme0n1p5", 195_312_500, 1_367_187_501, 512),
                KernelDeviceIdentity(3, disk_path / "nvme0n1p4", 976_896, 1_365_234_375, 512),
            )

    def test_executor_exposes_only_pinned_target_descriptor_to_mutating_commands(self):
        plan = build_apple_storage_plan(self.handoff)
        calls = []

        def runner(arguments, **options):
            calls.append((arguments, options))

        executor = AppleStorageExecutor(plan, runner=runner)
        executor._fds = {"disk": 40, "target": 41, "esp": 42}
        executor.revalidate = lambda: None
        executor.format_and_mount_target()

        self.assertEqual(len(calls), 2)
        for arguments, options in calls:
            self.assertIn("/proc/self/fd/41", arguments)
            self.assertNotIn("/proc/self/fd/40", arguments)
            self.assertNotIn("/proc/self/fd/42", arguments)
            self.assertEqual(options["pass_fds"], (41,))
            self.assertTrue(options["check"])

    def test_path_scoped_writer_replaces_only_grub_and_preserves_m1n1(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "EFI/BOOT").mkdir(parents=True)
            (root / "m1n1").mkdir()
            (root / "EFI/BOOT/BOOTAA64.EFI").write_bytes(b"old grub")
            (root / "m1n1/boot.bin").write_bytes(b"asahi boot object")
            content = b"signed grub"
            contract = _FakeBootContract(self.handoff.installation_id, content)

            install_authorized_grub(root, content, contract)

            self.assertEqual((root / "EFI/BOOT/BOOTAA64.EFI").read_bytes(), content)
            self.assertEqual((root / "m1n1/boot.bin").read_bytes(), b"asahi boot object")
            self.assertEqual(contract.authorized_paths, ["/EFI/BOOT/BOOTAA64.EFI"])

    def test_path_scoped_writer_rejects_symlinked_boot_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (root / "EFI").mkdir()
            (root / "EFI/BOOT").symlink_to(outside, target_is_directory=True)
            (root / "m1n1").mkdir()
            (root / "m1n1/boot.bin").write_bytes(b"asahi boot object")
            with self.assertRaises(OSError):
                install_authorized_grub(
                    root,
                    b"signed grub",
                    _FakeBootContract(self.handoff.installation_id, b"signed grub"),
                )
            self.assertFalse((outside / "BOOTAA64.EFI").exists())


class _FakeBootContract:
    def __init__(self, installation_id, expected_content):
        self.installation_id = installation_id
        self.expected_content = expected_content
        self.authorized_paths = []

    def authorize(self, path, content):
        if content != self.expected_content:
            raise HandoffError("unexpected content")
        self.authorized_paths.append(path)


if __name__ == "__main__":
    unittest.main()

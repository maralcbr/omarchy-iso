from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "builder/asahi_orchestrator_runner.py"
CONFIGURED_PROFILE = ROOT / "builder/asahi_orchestrator_configured.py"
FINALIZED_PROFILE = ROOT / "builder/asahi_orchestrator_finalized.py"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AsahiOrchestratorStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = load_module(RUNNER)
        self.configured_profile = load_module(CONFIGURED_PROFILE)
        self.finalized_profile = load_module(FINALIZED_PROFILE)

    def test_configured_stage_stops_before_boot_finalization(self) -> None:
        names = self.runner.stage_phase_names(self.configured_profile)
        self.assertEqual(
            names,
            [
                "Preparing live environment",
                "Preparing install target",
                "Installing Arch + Omarchy",
                "Configuring hibernation",
                "Configuring system",
                "Staging provisioning",
            ],
        )
        self.assertNotIn("Finalizing boot", names)

    def test_finalized_stage_starts_with_remount_and_includes_boot_validation(self) -> None:
        names = self.runner.stage_phase_names(self.finalized_profile)
        self.assertEqual(names[0], "Preparing install target")
        self.assertIn("Finalizing boot", names)
        self.assertIn("Validating boot setup", names)
        self.assertEqual(names[-1], "Creating factory snapshot")

    def test_finalized_stage_installs_vendor_firmware_persistently(self) -> None:
        """Apple Silicon WiFi firmware must be copied into the installed system.

        Regression: the first physical v7 install had no WiFi because the build
        relied on the upstream initramfs symlink to firmware in the EFI system
        partition. That link depends on the bootloader publishing an ESP
        pointer, and when it is absent the driver silently has no firmware.
        """
        profile = load_module(FINALIZED_PROFILE)
        phase_functions = [function for _, function in profile.PHASES]
        self.assertIn("install_vendor_firmware", phase_functions)
        self.assertLess(
            phase_functions.index("finalize_boot"),
            phase_functions.index("install_vendor_firmware"),
            "firmware install must follow boot finalization",
        )

        source = (
            ROOT
            / "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py"
        ).read_text()
        self.assertIn("VENDOR_FIRMWARE_UNIT", source)
        self.assertIn("def install_vendor_firmware(", source)
        # The unit must place a real copy in /lib/firmware rather than depend on
        # the ESP-backed symlink, and must survive firmware updates.
        self.assertIn("/boot/efi/vendorfw/firmware.tar", source)
        self.assertIn("-C /lib/firmware", source)
        self.assertIn("omarchy-vendor-firmware.service", source)
        self.assertIn("ConditionPathExists=/boot/efi/vendorfw/firmware.tar", source)

    def test_vendor_firmware_reloads_both_radios(self) -> None:
        """Wi-Fi and Bluetooth both probe before the firmware is extracted.

        Regression: the first boot after an M1 install had working Wi-Fi and
        dead Bluetooth. Both radios fail their early-boot firmware load, but
        the unit reloaded only brcmfmac, so hci_bcm4377 stayed unprobed
        ("Unable to load firmware ... error -2") until the next reboot.
        """
        source = (
            ROOT
            / "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py"
        ).read_text()
        post = [
            line
            for line in source.splitlines()
            if line.startswith("ExecStartPost=")
        ]
        self.assertEqual(len(post), 1, "expected exactly one ExecStartPost")
        for module in ("brcmfmac", "hci_bcm4377"):
            self.assertIn(module, post[0], f"{module} is never reloaded")

    def test_finalized_stage_enables_apple_platform_services(self) -> None:
        """Speaker safety must be enabled in the image.

        Regression: the 2026.08.31 package shipped with speakersafetyd.service
        present but disabled (Arch presets are "disable *" and no install
        script enables it), so the installed system booted with silent
        speakers.
        """
        profile = load_module(FINALIZED_PROFILE)
        phase_functions = [function for _, function in profile.PHASES]
        self.assertIn("enable_apple_platform_services", phase_functions)
        self.assertLess(
            phase_functions.index("install_vendor_firmware"),
            phase_functions.index("enable_apple_platform_services"),
            "platform services follow the firmware install",
        )

        source = (
            ROOT
            / "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py"
        ).read_text()
        self.assertIn("def enable_apple_platform_services(", source)
        self.assertIn("speakersafetyd.service", source)

    def test_timing_merge_preserves_both_stage_histories(self) -> None:
        configured = {
            "started_at": 10,
            "finished_at": 20,
            "phases": [{"name": "Configuring system", "status": "ok", "elapsed": 4}],
        }
        finalized = {
            "started_at": 30,
            "finished_at": 40,
            "phases": [{"name": "Finalizing boot", "status": "ok", "elapsed": 5}],
            "installed_packages": 918,
            "expected_packages": 919,
        }
        merged = load_module(FINALIZED_PROFILE).merge_timing(configured, finalized)
        self.assertEqual(merged["started_at"], 10)
        self.assertEqual(merged["finished_at"], 40)
        self.assertEqual(
            [phase["name"] for phase in merged["phases"]],
            ["Configuring system", "Finalizing boot"],
        )
        self.assertEqual(merged["installed_packages"], 918)

    def test_same_input_configured_runs_checkpoint_identical_semantic_state(self) -> None:
        checkpoint_states = []
        timing_evidence = []
        phase_names = [name for name, _function in self.configured_profile.PHASES]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_number in (1, 2):
                state_dir = root / f"run-{run_number}" / "state"
                target = root / f"run-{run_number}" / "target"
                target_log = target / "var/log/omarchy-install.log"
                target_timing = target / "var/log/omarchy-install-timing.json"
                state_dir.mkdir(parents=True)
                target_log.parent.mkdir(parents=True)
                raw_state = {
                    "started_at": 1_000.0 + run_number,
                    "finished_at": 2_000.0 + run_number,
                    "phase_started_at": 1_900.0 + run_number,
                    "target": f"/tmp/build-{run_number}/target",
                    "total_phases": len(phase_names),
                    "current_index": len(phase_names) - 1,
                    "current_phase": "Installation complete",
                    "phases": [
                        {
                            "name": name,
                            "status": "ok",
                            "elapsed": run_number + index / 10,
                        }
                        for index, name in enumerate(phase_names)
                    ],
                    "installed_packages": 918,
                    "expected_packages": 917,
                }
                (state_dir / "state.json").write_text(json.dumps(raw_state))
                (state_dir / "archinstall-user_configuration.json").write_text(
                    json.dumps({"mountpoint": raw_state["target"]})
                )
                target_log.write_text(f"started at {raw_state['started_at']}\n")
                target_timing.write_text(json.dumps(raw_state))
                evidence_path = root / "evidence" / f"configured-{run_number}.json"
                ctx = SimpleNamespace(state_dir=state_dir, target=target)

                with mock.patch.dict(
                    os.environ,
                    {"OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE": str(evidence_path)},
                    clear=False,
                ):
                    self.configured_profile.persist_stage_timing(
                        ctx,
                        raw_state,
                        None,
                        self.runner._atomic_json,
                    )

                checkpoint_states.append((state_dir / "state.json").read_bytes())
                timing_evidence.append(json.loads(evidence_path.read_text()))
                self.assertEqual(
                    [path.name for path in state_dir.iterdir()],
                    ["state.json"],
                )
                self.assertFalse(target_log.exists())
                self.assertFalse(target_timing.exists())

        self.assertEqual(checkpoint_states[0], checkpoint_states[1])
        self.assertNotEqual(timing_evidence[0], timing_evidence[1])
        self.assertEqual(timing_evidence[0]["timing"]["started_at"], 1_001.0)
        self.assertEqual(
            timing_evidence[1]["timing"]["target"],
            "/tmp/build-2/target",
        )

    def test_invalid_configured_state_does_not_publish_success_evidence(self) -> None:
        phase_names = [name for name, _function in self.configured_profile.PHASES]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_dir = root / "state"
            target = root / "target"
            evidence_path = root / "evidence" / "configured.json"
            state_dir.mkdir()
            target.mkdir()
            raw_state = {
                "total_phases": len(phase_names),
                "current_index": len(phase_names) - 1,
                "current_phase": "Installation complete",
                "phases": [
                    {
                        "name": name,
                        "status": "failed" if index == 0 else "ok",
                        "elapsed": 1.0,
                    }
                    for index, name in enumerate(phase_names)
                ],
                "installed_packages": 918,
                "expected_packages": 917,
            }
            ctx = SimpleNamespace(state_dir=state_dir, target=target)

            with mock.patch.dict(
                os.environ,
                {"OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE": str(evidence_path)},
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "incomplete or invalid"):
                    self.configured_profile.persist_stage_timing(
                        ctx,
                        raw_state,
                        None,
                        self.runner._atomic_json,
                    )

            self.assertFalse(evidence_path.exists())

    def test_same_input_finalized_runs_keep_timing_only_in_external_evidence(self) -> None:
        target_manifests = []
        run_evidence = []
        configured_timing = {
            "started_at": 100.0,
            "finished_at": 200.0,
            "target": "/tmp/configured-target",
            "phases": [
                {"name": "Configuring system", "status": "ok", "elapsed": 4.0}
            ],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured_evidence = root / "evidence" / "configured.json"
            configured_evidence.parent.mkdir()
            configured_evidence.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "verification_kind": "asahi-orchestrator-run-evidence-v1",
                        "stage": "configured-target",
                        "timing": configured_timing,
                    }
                )
            )

            for run_number in (1, 2):
                state_dir = root / f"run-{run_number}" / "state"
                target = root / f"run-{run_number}" / "target"
                state_dir.mkdir(parents=True)
                (target / "etc").mkdir(parents=True)
                (target / "etc/omarchy-release").write_text("stable\n")
                log_path = target / "var/log/omarchy-install.log"
                timing_path = target / "var/log/omarchy-install-timing.json"
                log_path.parent.mkdir(parents=True)
                log_path.write_text(f"run {run_number} at {300 + run_number}\n")
                timing_path.write_text(json.dumps({"run": run_number}))
                finalized_state = {
                    "started_at": 300.0 + run_number,
                    "finished_at": 400.0 + run_number,
                    "target": f"/tmp/finalized-{run_number}/target",
                    "phases": [
                        {
                            "name": "Finalizing boot",
                            "status": "ok",
                            "elapsed": 5.0 + run_number,
                        }
                    ],
                    "installed_packages": 918,
                    "expected_packages": 917,
                }
                finalized_evidence = (
                    root / "evidence" / f"finalized-{run_number}.json"
                )
                ctx = SimpleNamespace(state_dir=state_dir, target=target)
                with mock.patch.dict(
                    os.environ,
                    {
                        "OMARCHY_ASAHI_CONFIGURED_TIMING_EVIDENCE": str(
                            configured_evidence
                        ),
                        "OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE": str(
                            finalized_evidence
                        ),
                    },
                    clear=False,
                ):
                    prepared = self.finalized_profile.prepare_stage(ctx)
                    self.finalized_profile.persist_stage_timing(
                        ctx,
                        finalized_state,
                        prepared,
                        self.runner._atomic_json,
                    )

                self.assertFalse(log_path.exists())
                self.assertFalse(timing_path.exists())
                target_manifests.append(
                    {
                        str(path.relative_to(target)): path.read_bytes()
                        for path in target.rglob("*")
                        if path.is_file()
                    }
                )
                run_evidence.append(json.loads(finalized_evidence.read_text()))

        self.assertEqual(target_manifests[0], target_manifests[1])
        self.assertNotEqual(run_evidence[0], run_evidence[1])
        self.assertEqual(run_evidence[0]["stage"], "finalized-boot")
        self.assertEqual(run_evidence[0]["timing"]["started_at"], 100.0)
        self.assertEqual(run_evidence[1]["timing"]["finished_at"], 402.0)

    def test_image_runtime_routes_orchestrator_provenance_to_run_evidence(self) -> None:
        runtime = (ROOT / "builder/asahi-stages/image-runtime.sh").read_text()
        phases = (
            ROOT / "configs/airootfs/usr/share/omarchy-iso/orchestrator/phases.py"
        ).read_text()
        self.assertIn(
            'OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE="$run_evidence/',
            runtime,
        )
        self.assertIn(
            'OMARCHY_ASAHI_CONFIGURED_TIMING_EVIDENCE="$configured_timing_evidence"',
            runtime,
        )
        self.assertIn('OMARCHY_INSTALL_LOG_FILE="$run_evidence/', runtime)
        self.assertIn('OMARCHY_INSTALL_TIMING_FILE="$run_evidence/', runtime)
        self.assertNotIn('OMARCHY_INSTALL_LOG_FILE="$work/', runtime)
        self.assertIn("OMARCHY_INSTALL_TIMING_FILE", phases)

    def test_image_stages_reject_residual_orchestrator_run_evidence(self) -> None:
        for script_name, message in (
            (
                "configured-target.sh",
                "configured target retained volatile orchestrator run evidence",
            ),
            (
                "finalized-boot.sh",
                "finalized target retained volatile orchestrator run evidence",
            ),
        ):
            script = (ROOT / "builder/asahi-stages" / script_name).read_text()
            self.assertIn("var/log/omarchy-install.log", script)
            self.assertIn("var/log/omarchy-install-timing.json", script)
            self.assertIn(message, script)

    def test_stage_wrappers_bind_only_their_own_profile_and_phase_module(self) -> None:
        configured = (ROOT / "builder/run-asahi-configured-stage.py").read_text()
        finalized = (ROOT / "builder/run-asahi-finalized-stage.py").read_text()
        self.assertIn("asahi_orchestrator_configured", configured)
        self.assertIn("configured_phases", configured)
        self.assertNotIn("finalized", configured)
        self.assertIn("asahi_orchestrator_finalized", finalized)
        self.assertIn("finalized_phases", finalized)
        self.assertNotIn("configured as profile", finalized)

    def test_stage_media_reads_follow_the_bound_projection_root(self) -> None:
        configured = (
            ROOT
            / "configs/airootfs/usr/share/omarchy-iso/orchestrator/configured_phases.py"
        ).read_text()
        phases = (
            ROOT / "configs/airootfs/usr/share/omarchy-iso/orchestrator/phases.py"
        ).read_text()
        for text, names in (
            (
                configured,
                ("package-targets", "omarchy-base.packages", "install-debug"),
            ),
            (phases, ("expected-packages",)),
        ):
            self.assertIn("OMARCHY_ISO_MEDIA_ROOT", text)
            for name in names:
                self.assertNotIn(f"/usr/share/omarchy-iso/{name}", text)


if __name__ == "__main__":
    unittest.main()

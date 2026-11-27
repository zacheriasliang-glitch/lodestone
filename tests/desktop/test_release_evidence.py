# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Lodestone contributors
# License terms: ../../LICENSE
"""Authored receipt fixtures only; no installation, model, network or promotion."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import desktop_release_evidence as evidence

COMMIT = "a" * 40
SOURCE = {"smoke.py": b"authored source fixture\n", "dependencies.lock.json": b"mac-lock",
          "dependencies.ubuntu-24.04-x86_64.lock.json": b"linux-lock", "dependencies.windows-x86_64.lock.json": b"windows-lock"}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundles, self.files = [], {}
        for index, target in enumerate(evidence.TARGETS):
            bundle, files = self.fixture(target, index)
            self.bundles.append(bundle)
            self.files[target] = files

    def publish(self, target):
        directory = self.root / target / "recovered"
        directory.mkdir(parents=True, exist_ok=True)
        values = self.files[target]
        for relative, raw in values.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        inventory = {relative: {"bytes": len(raw), "sha256": evidence.sha(raw)} for relative, raw in values.items()}
        (directory / "EVIDENCE-MANIFEST.json").write_bytes(encoded({"schema": "lodestone-ci-log-files/v1", "files": inventory}))

    def fixture(self, target, index):
        directory = self.root / target
        directory.mkdir()
        payload = dict(SOURCE)
        payload["dependencies.lock.json"] = SOURCE[evidence.TARGETS[target][1]]
        manifest = {"schema": "lodestone-desktop-payload/v1", "target": target,
                    "platform": evidence.TARGETS[target][0], "architecture": "x86_64", "version": "fixture.1",
                    "files": {relative: evidence.sha(raw) for relative, raw in payload.items()}}
        manifest_raw = encoded(manifest)
        release = evidence.sha(manifest_raw)
        archive = directory / (target + ".zip")
        with zipfile.ZipFile(archive, "w") as output:
            for relative, raw in {**payload, "MANIFEST.json": manifest_raw}.items():
                output.writestr("Fixture/" + relative, raw)
        build = {"schema": "lodestone-desktop-build/v1", "target": target, "version": "fixture.1",
                 "archive": archive.name, "sha256": evidence.sha(archive.read_bytes()), "bytes": archive.stat().st_size,
                 "payload_files": len(payload), "model_weights_bundled": False, "private_user_records_bundled": False}
        build_path = directory / "BUILD.json"
        build_path.write_bytes(encoded(build))
        (directory / "SHA256SUMS.txt").write_bytes((build["sha256"] + "  " + build["archive"] + "\n").encode())
        native = evidence.PureWindowsPath("C:/fixture/preview") if target == "windows-x86_64" else evidence.PurePosixPath("/fixture/preview")
        installed = native / "releases" / release
        python = native / "python"
        active = {"schema": "lodestone-desktop-active/v1", "release": str(installed), "release_id": release,
                  "version": "fixture.1", "worker_python": str(python)}
        values = {}
        values["download/BUILD.json"] = build_path.read_bytes()
        values["download/" + archive.name] = archive.read_bytes()
        values["download/SHA256SUMS.txt"] = (directory / "SHA256SUMS.txt").read_bytes()
        def write(relative, value):
            values[relative] = encoded(value)
        write("portable-ci.json", {"schema": "lodestone-portable-ci/v1", "status": "PASSED", "target": target,
              "source_commit": COMMIT, "mode": "local_inference_smoke", "model_smoke_invoked": True,
              "paid_api_calls": 0, "weight_updates": 0, "semantic_learning": "NOT_ESTABLISHED"})
        write("source-checks.json", {"status": "PASSED", "passed": ["source-provenance"], "failed": []})
        for label in ("source-provenance", "build", "fresh-install", "gui-save-reopen", "gui-startup-1", "gui-startup-2",
                      "local-smoke-command", "idempotent-install", "synthetic-upgrade", "rollback", "restored-doctor", "uninstall"):
            write(label + ".json", {"status": "PASSED", "returncode": 0})
        write("download-identity.json", build)
        write("installed-active.json", active)
        write("current-installed-identity.json", {"schema": "lodestone-mechanical-installed-identity/v1",
              "status": "SOURCE_DOWNLOAD_AND_INSTALLED_BYTES_MATCH", "source_commit": COMMIT,
              "target": target, "release_id": release, "manifest_sha256": evidence.sha(manifest_raw),
              "files_verified": len(payload), "version": "fixture.1"})
        write("installed-gui-check.json", {"schema": "lodestone-installed-gui-check/v1", "status": "PASSED",
              "payload": str(installed), "tests": 27, "failures": 0, "errors": 0, "skipped": []})
        if target == "ubuntu-24.04-x86_64":
            write("native-gui-prerequisites.json", {"status": "PASSED", "returncode": 0})
            write("linux-gui-prerequisites/installed-gui-check.json", {
                "schema": "lodestone-installed-gui-check/v1", "status": "PASSED",
                "payload": str(installed), "tests": 2, "failures": 0, "errors": 0, "skipped": []})
        if target == "windows-x86_64":
            write("windows-long-path-policy-command.json", {"status": "PASSED", "returncode": 0})
            prior_policy = {"present": True, "type": 4, "value": 1}
            write("windows-long-path-policy.json", {
                "schema": "lodestone-windows-long-path-policy/v1", "status": "PASSED",
                "source_commit": COMMIT, "release_id": release,
                "original_policy": prior_policy, "restored_policy": prior_policy,
                "registry_restored": True,
                "policy_zero": {"effective_value": 0, "short_root_install": True, "doctor_ready": True,
                                "long_root_refused_before_mutation": True},
                "policy_one": {"deep_doctor_ready": True, "helper_explicit_yes": True, "effective_value": 1,
                               "long_root_preflight_passed": True}})
        for number, method in ((1, "direct_gui_python"), (2, evidence.TARGETS[target][2])):
            write("gui-startup-receipt-" + str(number) + ".json", {"launch_method": method, "receipt": {
                "schema": "lodestone-desktop-startup/v1", "release_id": release, "shown": True,
                "models": ["fixture"], "model_calls": 0}})
        correction = "Authored exact delivery fixture; no semantic claim."
        attempts = []
        for number in (1, 2):
            run = native / "Loom/runs" / ("fixture-" + str(number))
            prefix = "native/Loom/runs/fixture-" + str(number) + "/"
            worker = {"pid": 123 + number, "backend": "ollama_qwen3_base"}
            observation = {"source": "actual supervised local Loom worker", "endpoint": "http://127.0.0.1:11435",
                           "model_digest": "fixture-model", "token_count_matches": True, "done_reason": "stop",
                           "paid_api_calls": 0, "weight_updates": 0, "rendered_prompt_sha256": "b" * 64}
            supervision = {"stop": {"confirmed_stopped": True}, "rss_valid_sample_count": 2,
                           "observed_group_peak_rss_bytes": 1024}
            config = {"python": str(python), "backend": "ollama_qwen3_base", "ollama_model_digest": "fixture-model",
                      "max_rss_bytes": 134217728, "wall_seconds": 1200, "generation": {"max_tokens": 512},
                      "ollama_binary": str(native / "ollama.exe")}
            job = None
            if target == "windows-x86_64":
                worker.update(ownership="windows_job", filesystem_network_sandbox=False)
                image = evidence.sha(evidence.ntpath.normcase(evidence.ntpath.normpath(config["ollama_binary"])).encode())
                observation["server_pid"] = 456
                supervision.update(windows_job_commit_limit_bytes=config["max_rss_bytes"], windows_job_memory_limit_notified=False,
                    windows_job_member_evidence_truncated=False, windows_job_sampled_members=[
                        {"pid": worker["pid"], "image_path_sha256": "fixture-python"},
                        {"pid": 456, "image_path_sha256": image}, {"pid": 789, "image_path_sha256": image}])
                job = {"server_pid": 456, "sampled_runner_pids": [789], "confirmed_stopped": True}
            response = "Authored response " + str(number)
            write(prefix + "worker.json", worker)
            write(prefix + "result.json", {"status": "COMPLETED", "response": response, "observation": observation})
            write(prefix + "supervisor.json", {"status": "COMPLETED", "response": response, "observation": {"supervisor": supervision}})
            write(prefix + "config.json", config)
            write(prefix + "request.json", {"context": correction if number == 2 else "Authored first fixture"})
            receipt = {"artifacts_dir": str(run), "status": "COMPLETED", "provider_called": True,
                       "response": response, "outcome_handle": "fixture-outcome", "terminal_handle": "fixture-terminal"}
            write(prefix + "receipt.json", receipt)
            hashes = {p[len(prefix):]: evidence.sha(raw) for p, raw in values.items() if p.startswith(prefix)}
            verification = {"files": hashes, "artifacts_dir": str(run), "worker_pid": worker["pid"],
                            "response_sha256": evidence.sha(response.encode()), "model_digest": "fixture-model",
                            "rendered_prompt_sha256": "b" * 64}
            if job:
                verification["windows_job"] = job
            attempts.append({"encounter": number, "receipt": receipt, "verification": verification})
        write("local-smoke.json", {"schema": "lodestone-desktop-local-smoke/v1", "status": "PASSED",
              "release_id": release, "root": str(native), "paid_api_calls": 0, "weight_updates": 0,
              "semantic_learning": "NOT_ESTABLISHED", "max_encounters": 2, "max_tokens_per_encounter": 512, "attempts": attempts,
              "doctor": {"status": "READY", "release_id": release},
              "correction": {"text": correction, "sha256": evidence.sha(correction.encode()), "receipt": {"state": "RETAINED_UNRESOLVED"}},
              "delivery": {"exact_correction_in_second_request": True, "exact_correction_in_rendered_prompt": True,
                           "semantic_use_assessed": False, "rendered_prompt_sha256": "b" * 64}})
        retained = {relative.removeprefix("native/"): evidence.sha(raw) for relative, raw in values.items() if relative.startswith("native/")}
        write("retained-before-lifecycle.json", retained)
        write("lifecycle.json", {"status": "PASSED", "uninstalled": True, "original_release": release,
              "restored_release": release, "synthetic_upgrade_release": "c" * 64, "retained_files_verified": len(retained)})
        return [target, directory / "recovered", build_path, archive,
                "https://github.com/zacheriasliang-glitch/lodestone/actions/runs/123/job/" + str(index + 1)], values

    def assemble(self):
        for target in self.files:
            self.publish(target)
        return evidence.assemble(COMMIT, SOURCE, self.bundles)

    def edit(self, target, filename, change):
        value = json.loads(self.files[target][filename])
        change(value)
        self.files[target][filename] = encoded(value)

    def test_three_exact_native_fixture_trees_assemble_without_model_execution(self):
        result = self.assemble()
        self.assertEqual(result["targets_verified"], 3)
        self.assertEqual(result["actual_local_encounters"], 6)
        self.assertEqual(result["semantic_learning"], "NOT_ESTABLISHED")

    def test_mechanical_failed_foreign_commit_or_missing_target_cannot_be_released(self):
        target = next(iter(self.files))
        original = self.files[target]["portable-ci.json"]
        for field, value in (("mode", "mechanical_only"), ("status", "FAILED"), ("model_smoke_invoked", False),
                             ("source_commit", "d" * 40), ("paid_api_calls", 1)):
            with self.subTest(field=field):
                self.files[target]["portable-ci.json"] = original
                self.edit(target, "portable-ci.json", lambda report: report.update({field: value}))
                with self.assertRaises(ValueError):
                    self.assemble()
        with self.assertRaisesRegex(ValueError, "Exactly one"):
            evidence.assemble(COMMIT, SOURCE, self.bundles[:2])

    def test_source_or_released_archive_drift_is_rejected(self):
        self.assemble()
        with self.assertRaisesRegex(ValueError, "committed source"):
            evidence.assemble(COMMIT, {**SOURCE, "smoke.py": b"different source"}, self.bundles)
        self.bundles[0][3].write_bytes(b"changed download")
        with self.assertRaisesRegex(ValueError, "retained native bytes"):
            evidence.assemble(COMMIT, SOURCE, self.bundles)

    def test_native_zip_and_sidecar_must_be_retained_and_match_supplied_bytes(self):
        target = next(iter(self.files))
        original = copy.deepcopy(self.files[target])
        archive_name = self.bundles[0][3].name
        for filename in ("download/" + archive_name, "download/BUILD.json", "download/SHA256SUMS.txt"):
            with self.subTest(filename=filename):
                self.files[target] = {key: raw for key, raw in original.items() if key != filename}
                with self.assertRaises(ValueError):
                    self.assemble()
        self.files[target] = original
        self.assemble()
        self.bundles[0][2].with_name("SHA256SUMS.txt").write_bytes(b"changed checksum\n")
        with self.assertRaisesRegex(ValueError, "checksum sidecar"):
            evidence.assemble(COMMIT, SOURCE, self.bundles)

    def test_bound_receipts_still_fail_for_bad_cleanup_gui_exit_or_delivery(self):
        target = "windows-x86_64"
        original = copy.deepcopy(self.files[target])
        for filename, change in (("gui-startup-1.json", lambda v: v.update(returncode=-11)),
                ("windows-long-path-policy-command.json", lambda v: v.update(returncode=1)),
                ("local-smoke.json", lambda v: v["delivery"].update(exact_correction_in_second_request=False)),
                ("local-smoke.json", lambda v: v["attempts"][0]["verification"]["windows_job"].update(confirmed_stopped=False)),
                ("local-smoke.json", lambda v: v["attempts"][0]["receipt"].update(artifacts_dir="C:/foreign/run"))):
            with self.subTest(filename=filename):
                self.files[target] = copy.deepcopy(original)
                self.edit(target, filename, change)
                with self.assertRaises(ValueError):
                    self.assemble()

    def test_tampered_native_run_cannot_hide_behind_passed_smoke_status(self):
        target = "windows-x86_64"
        self.edit(target, "native/Loom/runs/fixture-1/supervisor.json",
                  lambda v: v["observation"]["supervisor"]["stop"].update(confirmed_stopped=False))
        with self.assertRaisesRegex(ValueError, "retained bytes"):
            self.assemble()

    def test_linux_prerequisite_receipts_must_pass_without_skips_for_installed_payload(self):
        target = "ubuntu-24.04-x86_64"
        original = copy.deepcopy(self.files[target])
        receipt = "linux-gui-prerequisites/installed-gui-check.json"
        for filename, change in (
            ("native-gui-prerequisites.json", lambda v: v.update(returncode=1)),
            (receipt, lambda v: v.update(errors=1)),
            (receipt, lambda v: v.update(skipped=["missing library"])),
            (receipt, lambda v: v.update(payload="/foreign/installation")),
        ):
            with self.subTest(filename=filename, change=change):
                self.files[target] = copy.deepcopy(original)
                self.edit(target, filename, change)
                with self.assertRaises(ValueError):
                    self.assemble()
        self.files[target] = {key: raw for key, raw in original.items() if key != receipt}
        with self.assertRaises(ValueError):
            self.assemble()

    def test_windows_prerequisite_requires_actual_paths_helper_and_restored_policy(self):
        target = "windows-x86_64"
        filename = "windows-long-path-policy.json"
        original = copy.deepcopy(self.files[target])
        for change in (
            lambda v: v.update(status="FAILED"),
            lambda v: v.update(source_commit="d" * 40),
            lambda v: v.update(release_id="d" * 64),
            lambda v: v["policy_zero"].update(effective_value=1),
            lambda v: v["policy_zero"].update(short_root_install=False),
            lambda v: v["policy_zero"].update(long_root_refused_before_mutation=False),
            lambda v: v["policy_one"].update(helper_explicit_yes=False),
            lambda v: v["policy_one"].update(effective_value=0),
            lambda v: v.update(registry_restored=False),
            lambda v: v["restored_policy"].update(present=False, type=None, value=None),
        ):
            with self.subTest(change=change):
                self.files[target] = copy.deepcopy(original)
                self.edit(target, filename, change)
                with self.assertRaises(ValueError):
                    self.assemble()
        self.files[target] = {key: raw for key, raw in original.items() if key != filename}
        with self.assertRaises(ValueError):
            self.assemble()

    def test_rehashed_failed_cleanup_and_old_clobbered_smoke_shape_are_rejected(self):
        target = "windows-x86_64"
        relative = "Loom/runs/fixture-1/supervisor.json"
        self.edit(target, "native/" + relative,
                  lambda v: v["observation"]["supervisor"]["stop"].update(confirmed_stopped=False))
        digest = evidence.sha(self.files[target]["native/" + relative])
        self.edit(target, "retained-before-lifecycle.json", lambda v: v.update({relative: digest}))
        self.edit(target, "local-smoke.json", lambda v: v["attempts"][0]["verification"]["files"].update({"supervisor.json": digest}))
        with self.assertRaisesRegex(ValueError, "cleanup/RSS"):
            self.assemble()
        self.files[target]["local-smoke.json"] = encoded({"status": "PASSED", "returncode": 0, "command": ["fixture"]})
        with self.assertRaisesRegex(ValueError, "smoke scope"):
            self.assemble()

    def test_recovered_hash_drift_and_unsafe_inventory_are_rejected(self):
        self.assemble()
        root = self.bundles[0][1]
        (root / "portable-ci.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            evidence.recovered(root)
        self.publish(self.bundles[0][0])
        inventory = json.loads((root / "EVIDENCE-MANIFEST.json").read_bytes())
        inventory["files"]["../outside"] = {"bytes": 0, "sha256": evidence.sha(b"")}
        (root / "EVIDENCE-MANIFEST.json").write_bytes(encoded(inventory))
        with self.assertRaises(ValueError):
            evidence.recovered(root)


if __name__ == "__main__":
    unittest.main()

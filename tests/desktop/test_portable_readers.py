"""Actual bounded catalogue readers with local inert inputs; no model calls."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery.loom_operations import OperationBridge
from lodestone_machinery import loom_optional_operations as optional
from lodestone_machinery.fold_export import regular_bytes


class PortableReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Loom readers ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "uncreated workspace"
        self.bridge = OperationBridge(RUNTIME, self.root / "receipts")
        self.payload = "Attributed inert bytes: 水\n".encode("utf-8")
        self.packet = {"schema": "lodestone-mailbox/v1", "id": "fixture-envelope", "to": "cloud", "kind": "file",
            "size": len(self.payload), "sha256": hashlib.sha256(self.payload).hexdigest(),
            "payload_b64": base64.b64encode(self.payload).decode("ascii")}
        self.envelope = self.root / "envelope ü.json"
        self.envelope.write_text(json.dumps(self.packet), encoding="utf-8")
        self.specification = self.root / "planning.json"
        self.planning = {"schema": "loom-inference-memory-input/v1",
            "model_config": {"model_type": "qwen3", "tie_word_embeddings": True,
                "hidden_size": 8, "intermediate_size": 16, "num_hidden_layers": 2,
                "num_attention_heads": 2, "num_key_value_heads": 1, "head_dim": 4, "vocab_size": 32},
            "prompt_tokens": 64, "max_new_tokens": 32, "max_rss_bytes": 1024 * 1024,
            "prefill_chunk_tokens": 16}
        self.specification.write_text(json.dumps(self.planning), encoding="utf-8")

    def invoke(self, operation, inputs):
        request = self.bridge.prepare(operation, inputs, self.workspace)
        result = self.bridge.run(request, approved_digest=request["digest"])
        self.assertTrue(result["cleanup"]["confirmed_stopped"], result)
        self.assertTrue(result["output_capture_stopped"], result)
        self.assertFalse(self.workspace.exists())
        return request, result

    def mailbox_inputs(self, **overrides):
        return {"envelope": str(self.envelope), "expected_id": "fixture-envelope", "destination": "cloud", **overrides}

    def test_actual_mailbox_validation_preserves_exact_bytes_without_transport(self):
        before = self.envelope.read_bytes()
        request, result = self.invoke("mailbox.validate-envelope", self.mailbox_inputs())
        self.assertEqual(result["status"], "COMPLETED", result)
        report = json.loads(result["output"])
        self.assertEqual(report["status"], "VALIDATED")
        self.assertEqual(report["payload_sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(report["payload_bytes"], len(self.payload))
        self.assertEqual(request["input_sha256"][str(self.envelope)], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.envelope.read_bytes(), before)
        self.assertFalse((self.root / "mailbox").exists())

    def test_actual_malformed_envelope_and_destination_failures_are_retained(self):
        cases = [
            (json.dumps({**self.packet, "sha256": "0" * 64}), self.mailbox_inputs()),
            (json.dumps(self.packet), self.mailbox_inputs(destination="mac")),
            ('{"schema":"first",' + json.dumps(self.packet)[1:], self.mailbox_inputs()),
        ]
        for raw, inputs in cases:
            self.envelope.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw):
                _, result = self.invoke("mailbox.validate-envelope", inputs)
                self.assertEqual(result["status"], "FAILED", result)
                self.assertEqual(json.loads(result["output"])["status"], "ERROR")
                self.assertEqual(self.envelope.read_text(encoding="utf-8"), raw)
                retained = json.loads((Path(result["receipt_directory"]) / "result.json").read_bytes())
                self.assertEqual(retained, result)

    def test_actual_assessment_returns_only_attributed_planning_estimates(self):
        before = self.specification.read_bytes()
        _, result = self.invoke("loom.inference-memory-assess", {"specification": str(self.specification)})
        self.assertEqual(result["status"], "COMPLETED", result)
        report = json.loads(result["output"])
        self.assertEqual(report["input_file_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(report["input_attribution"], "CALLER_SUPPLIED_PLANNING_DATA")
        self.assertFalse(report["model_identity_verified"])
        self.assertFalse(report["rss_fit_guaranteed"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["weight_updates"], 0)
        self.assertEqual(report["assessment"]["cache_capacity_tokens"], 95)
        self.assertFalse(report["assessment"]["fit_established"])
        self.assertEqual(self.specification.read_bytes(), before)

    def test_assessment_import_closure_does_not_load_model_libraries(self):
        script = """
import sys
sys.path.insert(0, sys.argv[1])
class RefuseModels:
    def find_spec(self, name, *args):
        if name in {'resource', 'fcntl'}:
            raise ImportError('POSIX-only module is unavailable')
        if name.split('.')[0] in {'torch', 'transformers', 'ollama', 'openai', 'anthropic'}:
            raise AssertionError('Unexpected model/provider import: ' + name)
sys.meta_path.insert(0, RefuseModels())
from lodestone_machinery.loom_memory_cli import main
raise SystemExit(main(['assess', '--file', sys.argv[2]]))
"""
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(RUNTIME / "src"),
                                 str(self.specification)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["model_calls"], 0)

    def test_actual_invalid_planning_geometry_fails_without_altering_input(self):
        self.planning["prompt_tokens"] = 8192
        self.planning["max_new_tokens"] = 512
        self.specification.write_text(json.dumps(self.planning), encoding="utf-8")
        before = self.specification.read_bytes()
        _, result = self.invoke("loom.inference-memory-assess", {"specification": str(self.specification)})
        self.assertEqual(result["status"], "FAILED", result)
        self.assertIn("Complete prompt and reserved output", result["output"])
        self.assertEqual(self.specification.read_bytes(), before)

    def test_read_bounds_and_single_link_contract_are_preserved(self):
        oversized = self.root / "oversized"
        oversized.write_bytes(b"x" * 65537)
        with self.assertRaises(ValueError):
            regular_bytes(oversized, 65536)
        hardlink = self.root / "same-file"
        os.link(self.specification, hardlink)
        with self.assertRaisesRegex(ValueError, "singly linked"):
            regular_bytes(self.specification, 65536)
        with self.assertRaises(ValueError):
            optional._read(oversized, 65536)

    @unittest.skipUnless(os.name == "nt", "Native Windows reader custody")
    def test_windows_direct_planning_cli_rejects_raw_dot_space_and_stream_names(self):
        before = self.specification.read_bytes()
        for suffix in (".", " ", ":stream"):
            command = [sys.executable, "-B", str(RUNTIME / "lodestone.py"), "--repo", str(RUNTIME),
                       "--workspace", str(self.workspace), "loom-tools", "inference-memory", "assess",
                       "--file", str(self.specification) + suffix]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("ambiguous path", result.stderr)
        self.assertEqual(self.specification.read_bytes(), before)
        self.assertFalse(self.workspace.exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows reader custody")
    def test_windows_direct_cli_does_not_resolve_selected_package_or_workspace_junction(self):
        for name, target in (("package alias", RUNTIME), ("workspace alias", self.root)):
            junction = self.root / name
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(os.rmdir, junction)
            command = [sys.executable, "-B", str(RUNTIME / "lodestone.py"),
                       "--repo", str(junction if name == "package alias" else RUNTIME),
                       "--workspace", str(junction if name == "workspace alias" else self.workspace),
                       "loom-tools", "inference-memory", "assess", "--file", str(self.specification)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("reparse", result.stderr)
        self.assertFalse(self.workspace.exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows reader custody")
    def test_windows_junction_inputs_and_trusted_loader_paths_are_refused(self):
        (self.root / "inert.py").write_text("raise AssertionError('Aliased source must not execute')\n")
        junction = self.root / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(self.root)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, junction)
        with self.assertRaises((ValueError, OSError)):
            optional.execute("mailbox.validate-envelope", self.mailbox_inputs(envelope=str(junction / self.envelope.name)),
                             root=RUNTIME, workspace=self.workspace)
        with self.assertRaises((ValueError, OSError)):
            regular_bytes(junction / self.specification.name, 65536)
        with self.assertRaises((ValueError, OSError)):
            optional._load(junction, "inert.py", "_refused_junction_reader")
        self.assertFalse(self.workspace.exists())


if __name__ == "__main__":
    unittest.main()


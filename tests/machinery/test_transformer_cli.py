"""Actual CLI, record custody and a literal bridge from an Origami fixture answer."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lodestone_machinery import Origami, RecordStore
from lodestone_machinery import transformer
from lodestone_machinery import transformer_cli


RUNNER = Path(__file__).resolve().parents[2] / "tools/machinery/run"


class TransformerCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "workspace"
        self.model = self.workspace / "model.json"

    def invoke(self, operation, *extra, workspace=None, model=None):
        return subprocess.run([sys.executable, "-B", str(RUNNER), "origami", "transformer", operation,
                               "--workspace", str(workspace or self.workspace),
                               "--model", str(model or self.model), *extra], capture_output=True, text=True)

    def initialize(self, *extra):
        process = self.invoke("init", "--vocabulary-json", '["a","b","c"," "]',
                              "--width", "4", "--heads", "2", "--max-tokens", "8", "--seed", "7", *extra)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def records(self):
        return {path.name: path.read_bytes() for path in self.workspace.glob("organs/*/records/*.json")}

    def fold(self):
        sources = self.base / "sources"
        sources.mkdir()
        source = sources / "literal.txt"
        source.write_text("a\n")
        organ = Origami(self.workspace)
        run_id = organ.start(sources, [source.name], "Which character is recorded?", "unit-test",
                            "Read the explicit unit-test fixture only",
                            roles={"constructor": "fixture-constructor", "consumer": "fixture-consumer",
                                   "checker": "fixture-checker", "provider": "fixture-constructor"})["run_id"]
        request = organ.request(run_id)
        response = {"request_id": request["request_id"], "decision": "ANSWER",
                    "claims": [{"id": "literal", "subject": "fixture.character", "value": "a",
                                "scope": "literal.txt", "epoch": "unit-test", "basis": "RECORDED",
                                "support": [request["evidence"][0]["id"]]}],
                    "relations": [], "alternatives": [], "answer": "a", "answer_claim_ids": ["literal"],
                    "next_check": None, "coverage_assumptions": ["Literal fixture only."], "unresolved": []}
        result = organ.advance(run_id, {"provider": "fixture-constructor", "model": "NONE",
                                       "response_id": None, "usage": None, "response": response})
        self.assertEqual(result["status"], "ANSWER")
        return run_id, source, result

    def test_real_dispatch_initialization_forward_generation_and_catalog(self):
        initial = self.initialize()
        self.assertEqual(initial["status"], "INITIALIZED")
        self.assertEqual(initial["provenance"]["training_status"], "UNTRAINED")
        original = self.model.read_bytes()
        store = RecordStore(self.workspace)
        saved_model = store.read(initial["model_record"])
        self.assertEqual(saved_model["owner"], "origami")
        self.assertEqual(saved_model["payload"]["model_sha256"], transformer.model_sha256(transformer.load_model(self.model)))
        handles = [initial["record"]]
        for operation, options in (("forward", []), ("generate", ["--max-new-tokens", "3"])):
            process = self.invoke(operation, "--token-ids-json", "[0,1]", *options)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result["status"], "COMPUTED")
            self.assertEqual(result["result"]["semantic_validation"], "NOT_ESTABLISHED")
            self.assertEqual(result["result"]["model_sha256"], initial["model"]["sha256"])
            record = store.read(result["record"])
            self.assertEqual(record["kind"], "transformer_" + operation)
            self.assertEqual(set(record["links"]), {result["model_record"], result["input_record"]})
            self.assertEqual(record["payload"]["result"], result["result"])
            self.assertEqual(store.read(result["input_record"])["payload"]["token_ids"], [0, 1])
            self.assertTrue(result["result"]["attention"] if operation == "forward" else result["result"]["trace"])
            handles.append(result["record"])
        self.assertEqual(store.verify_links(handles)["status"], "INTACT")
        self.assertEqual(store.catalog()["verdict"], "OK")
        self.assertEqual({entry["owner"] for entry in store.catalog()["entries"]}, {"origami"})
        self.assertEqual(self.model.read_bytes(), original)

    def test_token_file_record_retains_exact_input_after_source_change(self):
        self.initialize()
        path = self.base / "tokens.json"
        raw = b"[0, 2]\n"
        path.write_bytes(raw)
        process = self.invoke("forward", "--tokens-file", str(path))
        self.assertEqual(process.returncode, 0, process.stderr)
        output = json.loads(process.stdout)
        path.write_text("[1, 1]")
        payload = RecordStore(self.workspace).read(output["input_record"])["payload"]
        self.assertEqual(payload["token_ids"], [0, 2])
        self.assertEqual(payload["source"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(payload["source"]["json_text"].encode(), raw)

    def test_invalid_inputs_create_no_records_or_new_workspace(self):
        self.initialize()
        for index, options in enumerate((
                ["--token-ids-json", "[true]"], ["--token-ids-json", "[4]"],
                ["--token-ids-json", "[]"], ["--token-ids-json", "[0]", "--max-new-tokens", "8"],
                ["--token-ids-json", "[0]", "--eos-token-id", "4"],
                ["--token-ids-json", "not-json"])):
            workspace = self.base / ("invalid-" + str(index))
            with self.subTest(options=options):
                process = self.invoke("generate", *options, workspace=workspace)
                self.assertEqual(process.returncode, 2)
                self.assertIn("error", json.loads(process.stderr))
                self.assertFalse(workspace.exists())

    def test_initialization_does_not_overwrite_or_escape_workspace(self):
        self.initialize()
        before, records = self.model.read_bytes(), self.records()
        repeat = self.invoke("init", "--vocabulary-json", '["a","b"]')
        self.assertEqual(repeat.returncode, 2)
        self.assertEqual(self.model.read_bytes(), before)
        self.assertEqual(self.records(), records)
        outside = self.base / "outside.json"
        escaped = self.invoke("init", "--vocabulary-json", '["a","b"]', model=outside)
        self.assertEqual(escaped.returncode, 2)
        self.assertFalse(outside.exists())
        self.assertEqual(self.records(), records)

    def test_model_tamper_and_oversize_token_file_publish_nothing(self):
        self.initialize()
        records = self.records()
        large = self.base / "large.json"
        large.write_text(" " * 4097)
        process = self.invoke("forward", "--tokens-file", str(large))
        self.assertEqual(process.returncode, 2)
        self.assertEqual(self.records(), records)
        envelope = json.loads(self.model.read_text())
        envelope["model"]["token_embeddings"][0][0] += 1
        self.model.write_text(json.dumps(envelope))
        process = self.invoke("forward", "--token-ids-json", "[0]")
        self.assertEqual(process.returncode, 2)
        self.assertIn("hash mismatch", json.loads(process.stderr)["message"])
        self.assertEqual(self.records(), records)

    def test_existing_fold_answer_is_literal_input_with_preserved_custody(self):
        self.initialize()
        run_id, source, result = self.fold()
        directory = self.workspace / "origami" / run_id
        before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        process = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(process.returncode, 0, process.stderr)
        output = json.loads(process.stdout)
        payload = RecordStore(self.workspace).read(output["input_record"])["payload"]
        self.assertEqual(payload["token_ids"], [0])
        self.assertEqual(payload["source"]["text"], "a")
        self.assertEqual(payload["source"]["result_sha256"], hashlib.sha256(before["result.json"]).hexdigest())
        self.assertEqual(payload["source"]["intent_sha256"], hashlib.sha256(before["intent.json"]).hexdigest())
        self.assertEqual(payload["source"]["artifact_sha256"], hashlib.sha256(before["answer.md"]).hexdigest())
        self.assertEqual(payload["source"]["sources"], result["scope"]["sources"])
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}, before)
        records = self.records()
        source.write_text("b\n")
        stale = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(stale.returncode, 2)
        self.assertIn("current sources", json.loads(stale.stderr)["message"])
        self.assertEqual(self.records(), records)
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}, before)

    def test_damaged_fold_answer_and_missing_vocabulary_are_rejected(self):
        self.initialize()
        run_id, _, result = self.fold()
        alternate = transformer.initialize_model(["b", "c"], width=4, heads=2, max_tokens=8)
        alternate_path = self.workspace / "alternate.json"
        transformer.save_model(alternate, alternate_path)
        records = self.records()
        missing = self.invoke("forward", "--fold-run", run_id, model=alternate_path)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("absent", json.loads(missing.stderr)["message"])
        Path(result["artifact"]).write_text("Changed artifact")
        damaged = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(damaged.returncode, 2)
        self.assertIn("intact", json.loads(damaged.stderr)["message"])
        self.assertEqual(self.records(), records)

    def test_changed_result_answer_is_rejected_before_publication(self):
        self.initialize()
        run_id, _, result = self.fold()
        result["answer"] = "b"
        path = self.workspace / "origami" / run_id / "result.json"
        path.write_text(json.dumps(result))
        records = self.records()
        process = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(process.returncode, 2)
        self.assertIn("answer differs", json.loads(process.stderr)["message"])
        self.assertEqual(self.records(), records)

    def test_changed_result_scope_is_rejected_before_publication(self):
        self.initialize()
        run_id, _, result = self.fold()
        result["scope"]["sources"][0]["sha256"] = "0" * 64
        path = self.workspace / "origami" / run_id / "result.json"
        path.write_text(json.dumps(result))
        records = self.records()
        process = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(process.returncode, 2)
        self.assertIn("scope differs", json.loads(process.stderr)["message"])
        self.assertEqual(self.records(), records)

    def test_jointly_changed_answer_and_fold_fail_rendered_artifact_check(self):
        self.initialize()
        run_id, _, result = self.fold()
        result["answer"] = result["fold"]["answer"] = "b"
        path = self.workspace / "origami" / run_id / "result.json"
        path.write_text(json.dumps(result))
        records = self.records()
        process = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(process.returncode, 2)
        self.assertIn("rendered result", json.loads(process.stderr)["message"])
        self.assertEqual(self.records(), records)

    def test_changed_result_run_id_is_rejected_before_publication(self):
        self.initialize()
        run_id, _, result = self.fold()
        result["run_id"] = "0" * 32
        path = self.workspace / "origami" / run_id / "result.json"
        path.write_text(json.dumps(result))
        records = self.records()
        process = self.invoke("forward", "--fold-run", run_id)
        self.assertEqual(process.returncode, 2)
        self.assertIn("requested run ID", json.loads(process.stderr)["message"])
        self.assertEqual(self.records(), records)

    def test_result_changed_during_inspection_is_rejected_before_publication(self):
        self.initialize()
        run_id, _, _ = self.fold()
        path = self.workspace / "origami" / run_id / "result.json"
        records = self.records()
        inspect = Origami.inspect

        def concurrent_change(organ, identifier):
            result = inspect(organ, identifier)
            changed = json.loads(path.read_text())
            changed["answer"] = "b"
            path.write_text(json.dumps(changed))
            return result

        stderr = io.StringIO()
        with patch.object(Origami, "inspect", concurrent_change), contextlib.redirect_stderr(stderr):
            with contextlib.redirect_stdout(io.StringIO()):
                code = transformer_cli.main(["forward", "--workspace", str(self.workspace),
                                             "--model", str(self.model), "--fold-run", run_id])
        self.assertEqual(code, 2)
        self.assertIn("changed while inspecting", json.loads(stderr.getvalue())["message"])
        self.assertEqual(self.records(), records)


if __name__ == "__main__":
    unittest.main()

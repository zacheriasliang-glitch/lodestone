"""External-source freshness falsifiers with explicitly scripted support reviews.

No model, network, daemon or subprocess runs in this module. Fixture verdicts
exercise custody; they do not establish independent semantic skill.
"""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from lodestone_machinery import macro_voxel as mv
from lodestone_machinery import yggdrasil_runtime as yr
from lodestone_machinery.origami import Origami
from lodestone_machinery.records import RecordStore
from lodestone_machinery.support_review import publish_support_review, support_request
from lodestone_machinery.yggdrasil_sources import BoundSourceError, read_bound_sources
from test_macro_voxel import fixture


class YggdrasilSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.lab = self.base / "lab"
        fixture(self.lab)
        self.capture = self.base / "capture"
        self.capture.mkdir()
        self.source = self.capture / "guide.txt"
        self.source.write_bytes(b"Current label: blue.\nMode: paper.\n")
        self.organ = Origami(self.base / "workspace")
        self.store = RecordStore(self.organ.workspace)
        self.roles = {"constructor": "fixture-constructor", "consumer": "fixture-consumer",
                      "checker": "fixture-checker", "provider": "fixture-provider"}
        self.receipt = self.complete(self.capture)

    def complete(self, root, *, label="blue", verdict="PASS"):
        run = self.organ.start(root, ["guide.txt"], "Which label?", "fixture-current",
                               "bounded fixture source read", roles=self.roles)
        request = self.organ.request(run["run_id"])
        response = {"request_id": request["request_id"], "decision": "ANSWER",
                    "claims": [{"id": "label", "subject": "label", "value": label,
                                "scope": "fixture only", "epoch": "fixture-current", "basis": "RECORDED",
                                "support": [request["evidence"][0]["id"]]}],
                    "relations": [], "alternatives": [], "answer": "The label is " + label + ".",
                    "answer_claim_ids": ["label"], "next_check": None,
                    "coverage_assumptions": ["Only this scripted fixture is in scope."], "unresolved": []}
        self.organ.advance(run["run_id"], {"provider": "fixture-provider", "model": "NONE",
                                          "response_id": None, "usage": None, "response": response})
        packet = support_request(self.organ, run["run_id"])
        review = {"schema": "origami-support-review/v1", "review_request_sha256": packet["request_sha256"],
                  "reviewer": "fixture-checker", "claim_verdicts": [
                      {"id": "label", "verdict": "SUPPORTED" if verdict == "PASS" else "UNSUPPORTED",
                       "reason": "Scripted fixture judgment."}],
                  "answer_verdict": verdict, "omissions": [], "contradictions": [],
                  "applicability": "SAME_QUESTION", "reason": "Scripted fixture review only."}
        return publish_support_review(self.organ, run["run_id"], review)

    def bindings(self):
        return {self.receipt["source_handle"]: self.capture}

    def saved(self, **kwargs):
        arguments = {"records_workspace": self.organ.workspace, "capture_roots": self.bindings(),
                     "record_handles": [self.receipt["handle"]]}
        arguments.update(kwargs)
        return yr.snapshot(self.lab, **arguments)

    def reopen(self, voxel, **kwargs):
        arguments = {"records_workspace": self.organ.workspace, "capture_roots": self.bindings()}
        arguments.update(kwargs)
        return yr.reenter(voxel, self.lab, **arguments)

    def bound_record(self):
        return self.store.read(self.receipt["source_handle"])

    def test_source_passage_claim_and_review_chain_is_inspectable(self):
        voxel = self.saved()
        graph = mv.decode(voxel)
        self.assertEqual(self.reopen(voxel)["freshness"], "CURRENT")
        anchors = yr.record_anchors(graph)
        self.assertEqual(set(anchors), {self.receipt[k] for k in ("handle", "source_handle", "answer_handle")})
        kinds = {edge["kind"] for edge in graph["edges"]}
        self.assertTrue({"contains_passage", "bound_passage", "contains_claim", "claim_support",
                         "claim_review:SUPPORTED", "record_dependency"} <= kinds)
        ancestry = mv.expand(voxel, anchors[self.receipt["handle"]], direction="ancestors", depth=6)
        self.assertTrue(any(n["role"] == "origami_passage" for n in ancestry["nodes"]))
        self.assertTrue(any(n["role"] == "origami_claim" for n in ancestry["nodes"]))
        check = self.reopen(voxel)["source_checks"][0]
        self.assertEqual(check["read_bytes"], self.source.stat().st_size)
        self.assertEqual(check["passages_checked"], len(self.bound_record()["payload"]["evidence"]))

    def test_changed_external_bytes_are_stale_even_when_records_are_current(self):
        voxel = self.saved()
        self.source.write_bytes(b"Current label: red.\nMode: paper.\n")
        result = self.reopen(voxel)
        self.assertEqual(result["freshness"], "STALE")
        self.assertEqual(result["source_checks"][0]["code"], "SOURCE_DRIFT")
        self.assertNotEqual(result["source_checks"][0]["expected_sha256"],
                            result["source_checks"][0]["observed_sha256"])

    def test_missing_explicit_root_never_follows_attributed_root(self):
        voxel = self.saved()
        result = self.reopen(voxel, capture_roots={})
        self.assertEqual(result["freshness"], "UNAVAILABLE")
        self.assertEqual(result["source_checks"][0]["read_bytes"], 0)
        with self.assertRaisesRegex(ValueError, "explicit capture root"):
            self.saved(capture_roots={})

    def test_missing_external_file_is_unavailable(self):
        voxel = self.saved()
        self.source.unlink()
        result = self.reopen(voxel)
        self.assertEqual(result["freshness"], "UNAVAILABLE")
        self.assertEqual(result["source_checks"][0]["status"], "UNAVAILABLE")

    def test_explicit_relocated_copy_is_current_after_original_disappears(self):
        voxel = self.saved()
        relocated = self.base / "relocated"
        shutil.copytree(self.capture, relocated)
        shutil.rmtree(self.capture)
        result = self.reopen(voxel, capture_roots={self.receipt["source_handle"]: relocated})
        self.assertEqual(result["freshness"], "CURRENT")
        self.assertEqual(self.reopen(voxel)["freshness"], "UNAVAILABLE")

    def test_legacy_record_only_snapshot_requires_external_capture(self):
        graph = mv.decode(self.saved())
        removed_sources = {key for key, source in graph["sources"].items() if source["scope"].startswith("capture:")}
        graph["sources"] = {key: value for key, value in graph["sources"].items() if key not in removed_sources}
        graph["scopes"] = {key: value for key, value in graph["scopes"].items() if not key.startswith("capture:")}
        graph["nodes"] = [n for n in graph["nodes"] if n["address"]["source_contract"] not in removed_sources]
        kept = {n["id"] for n in graph["nodes"]}
        graph["edges"] = [e for e in graph["edges"] if e["source"] in kept and e["target"] in kept]
        result = self.reopen(mv.encode(graph))
        self.assertEqual(result["freshness"], "UNAVAILABLE")
        self.assertIn("legacy", result["source_checks"][0]["reason"])

    def test_same_relative_path_in_different_roots_keeps_distinct_provenance(self):
        second = self.base / "second"
        second.mkdir()
        (second / "guide.txt").write_bytes(b"Current label: amber.\nMode: paper.\n")
        receipt = self.complete(second, label="amber")
        mappings = {**self.bindings(), receipt["source_handle"]: second}
        graph = mv.decode(self.saved(record_handles=[self.receipt["handle"], receipt["handle"]], capture_roots=mappings))
        sources = {key: source for key, source in graph["sources"].items() if source["scope"].startswith("capture:")}
        self.assertEqual(len(sources), 2)
        self.assertEqual({x["path"] for x in sources.values()}, {"guide.txt"})
        self.assertEqual(len({x["sha256"] for x in sources.values()}), 2)
        passages = [n for n in graph["nodes"] if n["role"] == "origami_passage"]
        self.assertEqual({n["address"]["source_contract"] for n in passages}, set(sources))

    def test_wrong_root_cannot_borrow_same_named_file(self):
        voxel = self.saved()
        other = self.base / "wrong"
        other.mkdir()
        (other / "guide.txt").write_bytes(b"Current label: red.\nMode: paper.\n")
        self.assertEqual(self.reopen(voxel, capture_roots={self.receipt["source_handle"]: other})["freshness"], "STALE")
        with self.assertRaises(BoundSourceError):
            self.saved(capture_roots={self.receipt["source_handle"]: other})

    def test_symlink_source_and_alias_root_refuse(self):
        record = self.bound_record()
        alias = self.base / "alias"
        alias.symlink_to(self.capture, target_is_directory=True)
        with self.assertRaisesRegex(BoundSourceError, "alias"):
            read_bound_sources(record, alias)
        real = self.capture / "real.txt"
        self.source.rename(real)
        self.source.symlink_to(real)
        with self.assertRaisesRegex(BoundSourceError, "Symlink"):
            read_bound_sources(record, self.capture)

    def test_selected_closure_ignores_unrelated_watcher_append(self):
        voxel = self.saved()
        self.store.publish("watcher", "observation", {"status": "unrelated fixture", "authority": "NONE"})
        self.assertEqual(self.reopen(voxel)["freshness"], "CURRENT")

    def test_full_inventory_still_detects_append(self):
        voxel = self.saved(record_handles=[])
        self.store.publish("watcher", "observation", {"status": "new fixture", "authority": "NONE"})
        result = self.reopen(voxel)
        self.assertEqual(result["freshness"], "STALE")
        self.assertIn("Source inventory changed: records", result["differences"])

    def test_deleted_dependency_cannot_be_omitted_from_selected_closure(self):
        voxel = self.saved()
        self.store.path(self.receipt["answer_handle"]).unlink()
        self.assertEqual(self.reopen(voxel)["freshness"], "UNAVAILABLE")
        with self.assertRaises(ValueError):
            self.saved()

    def test_reentry_cannot_select_a_smaller_dependency_set(self):
        voxel = self.saved()
        result = self.reopen(voxel, record_handles=[self.receipt["source_handle"]])
        self.assertEqual(result["freshness"], "STALE")
        self.assertIn("Source inventory changed: records", result["differences"])

    def test_false_line_and_passage_metadata_are_invalid_after_counted_reads(self):
        for mutate in (lambda p: p["scope"]["sources"][0].update(lines=99),
                       lambda p: p["evidence"][0].update(text="invented passage"),
                       lambda p: p["evidence"][0].update(start_byte=0.0),
                       lambda p: p["evidence"][0].update(start_byte=False)):
            record = deepcopy(self.bound_record())
            mutate(record["payload"])
            with self.assertRaises(BoundSourceError) as raised:
                read_bound_sources(record, self.capture)
            self.assertEqual(raised.exception.code, "CAPTURE_INVALID")
            self.assertEqual(raised.exception.read_bytes, self.source.stat().st_size)
            self.assertEqual(raised.exception.sources_attempted, 1)

    def test_budget_preflight_refuses_before_any_open(self):
        record = self.bound_record()
        for arguments in ({"max_bytes": 1}, {"max_sources": 0}):
            with patch.object(Path, "open", side_effect=AssertionError("must not read")):
                with self.assertRaises(BoundSourceError) as raised:
                    read_bound_sources(record, self.capture, **arguments)
            self.assertEqual(raised.exception.code, "COVERAGE_LIMIT")
            self.assertEqual((raised.exception.read_bytes, raised.exception.sources_attempted), (0, 0))

    def test_drift_and_missing_file_account_for_attempted_reads(self):
        record = self.bound_record()
        self.source.write_bytes(b"Current label: red.\nMode: paper.\n")
        with self.assertRaises(BoundSourceError) as raised:
            read_bound_sources(record, self.capture)
        self.assertEqual(raised.exception.code, "SOURCE_DRIFT")
        self.assertEqual(raised.exception.read_bytes, self.source.stat().st_size)
        self.assertEqual(raised.exception.sources_attempted, 1)
        self.source.unlink()
        with self.assertRaises(OSError) as missing:
            read_bound_sources(record, self.capture)
        self.assertEqual((missing.exception.read_bytes, missing.exception.sources_attempted), (0, 1))

    def test_invalid_utf8_is_not_normalized(self):
        record = deepcopy(self.bound_record())
        raw = b"\xff\n"
        self.source.write_bytes(raw)
        record["payload"]["scope"]["sources"][0].update(sha256=mv.digest(raw), bytes=len(raw), lines=1)
        with self.assertRaises(BoundSourceError) as raised:
            read_bound_sources(record, self.capture)
        self.assertEqual(raised.exception.code, "CAPTURE_INVALID")
        self.assertEqual(raised.exception.read_bytes, len(raw))

    def test_malformed_duplicate_and_escaping_bindings_refuse_before_read(self):
        for mutate in (lambda p: p["scope"]["sources"].append(deepcopy(p["scope"]["sources"][0])),
                       lambda p: p["scope"]["sources"][0].update(path="../guide.txt"),
                       lambda p: p.update(source_root="relative"),
                       lambda p: p.update(authority="HOST")):
            record = deepcopy(self.bound_record())
            mutate(record["payload"])
            with self.assertRaises(BoundSourceError) as raised:
                read_bound_sources(record, self.capture)
            self.assertEqual(raised.exception.read_bytes, 0)
            self.assertEqual(raised.exception.sources_attempted, 0)

    def test_aggregate_capture_budget_is_checked_before_external_read(self):
        with patch.object(yr, "MAX_CAPTURE_BYTES", 1), patch.object(yr, "read_bound_sources", side_effect=AssertionError("must not read")):
            with self.assertRaisesRegex(ValueError, "aggregate snapshot"):
                self.saved()

    def test_current_failed_review_stays_failed(self):
        receipt = self.complete(self.capture, label="wrong", verdict="FAIL")
        voxel = self.saved(record_handles=[receipt["handle"]], capture_roots={receipt["source_handle"]: self.capture})
        result = self.reopen(voxel, capture_roots={receipt["source_handle"]: self.capture})
        self.assertEqual(result["freshness"], "CURRENT")
        graph = mv.decode(voxel)
        review = next(n for n in graph["nodes"] if n["id"] == yr.record_anchors(graph)[receipt["handle"]])
        self.assertEqual(review["state"]["payload"]["review"]["answer_verdict"], "FAIL")
        self.assertIn("not semantic support", result["freshness_scope"])

    def test_invalid_review_chain_is_rejected_even_with_valid_record_hashes(self):
        record = self.store.read(self.receipt["handle"])
        payload = deepcopy(record["payload"])
        payload["review"]["review_request_sha256"] = "0" * 64
        bad = self.store.publish("checker", "support_review", payload, links=record["links"])
        with self.assertRaisesRegex(ValueError, "different or changed"):
            self.saved(record_handles=[bad])

    def test_unknown_mapping_and_duplicate_cli_mapping_refuse(self):
        wrong = "origami:" + "0" * 64
        with self.assertRaises(ValueError):
            self.saved(capture_roots={**self.bindings(), wrong: self.capture})
        result = self.reopen(self.saved(), capture_roots={**self.bindings(), wrong: self.capture})
        self.assertEqual(result["freshness"], "UNAVAILABLE")
        value = self.receipt["source_handle"] + "=" + str(self.capture)
        with self.assertRaises(ValueError):
            yr.parse_capture_roots([value, value])

    def test_cli_inspection_and_exact_handle_expansion_run_in_process(self):
        path = self.base / "graph.json"
        path.write_text(json.dumps(self.saved()), encoding="utf-8")
        for argv in (["inspect", str(path)], ["expand", str(path), "--anchor", self.receipt["handle"],
                                                "--direction", "ancestors", "--depth", "6"]):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(yr.main(argv), 0)
            result = json.loads(output.getvalue())
            if argv[0] == "inspect":
                self.assertIn(self.receipt["handle"], result["record_anchors"])
            else:
                self.assertTrue(any(n["role"] == "origami_claim" for n in result["nodes"]))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(yr.main(["expand", str(path), "--anchor", "checker:" + "0" * 64]), 2)


if __name__ == "__main__":
    unittest.main()

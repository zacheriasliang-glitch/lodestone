"""Recovery, non-fusion and freshness falsifiers, including real producer records."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery import Engine, RecordStore, Origami
from lodestone_machinery import macro_voxel as mv
from lodestone_machinery import yggdrasil_runtime as yr


def fixture(root):
    for path in yr.REQUIRED | {"CURRENT_STATE.md", "src/lodestone_machinery/example.py"}:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Synthetic contract: " + path + "\n")
    (root / "receipts").mkdir()
    name = "20260101T000000Z__synthetic__RCP-20260101T000000Z-012345abcdef.md"
    record = {"receipt_id": "RCP-20260101T000000Z-012345abcdef", "mode": "write",
              "receipt_directory_path_set_before_creation": []}
    (root / "receipts" / name).write_text("```json\n" + json.dumps(record) + "\n```\n")
    manifest = {"schema": "yggdrasil-canon/v1", "status": "ACTIVE", "body_id": "fixture:lab",
                "authority": "HOST", "runtime_authority": "NONE", "adoption_ref": yr.ADOPTION,
                "scope": ["synthetic test only"],
                "bindings": {p: mv.digest((root / p).read_bytes()) for p in sorted(yr.REQUIRED)}}
    (root / yr.CANON).write_text(json.dumps(manifest))


def graph_fixture():
    sources = {"a": mv.source("fixture", "a.txt", b"Source A\x00\xff"),
               "b": mv.source("fixture", "b.txt", b"Source B")}
    a = yr.node("a", "fixture:lab", "epoch-1", "a", "builder", {"layer": "EX"}, "build-1")
    b = yr.node("b", "fixture:lab", "epoch-1", "b", "auditor", {"layer": "AP"}, "audit-2")
    a["selected_organ"], b["selected_organ"] = "builder", "auditor"
    a["phase"] = {"pulse_id": "one", "phase_name": "build", "ordinal": 1, "direction": "forward"}
    b["phase"] = {"pulse_id": "two", "phase_name": "audit", "ordinal": 2, "direction": "return"}
    b["contradictions"] = [{"claim": "a", "observed": "b", "evidence": "b"}]
    b["debt"] = [{"kind": "unpaid", "quantity": 3}]
    return {"schema": "yggdrasil-graph/v1", "body_id": "fixture:lab", "epoch": "epoch-1",
            "host": {"authority": "EXTERNAL", "approval_inherited": False},
            "scopes": {"fixture": {"kind": "synthetic", "paths": ["a.txt", "b.txt"]}},
            "sources": sources, "nodes": [a, b], "edges": [yr.edge("a", "b", "contradicts", ["b"])],
            "lineage": {"coverage": "synthetic"},
            "residue": mv.residue(debt=[{"amount": 3}], unknowns=["missing review"])}


class CodecTests(unittest.TestCase):
    def test_lossless_projection_and_binary_provenance(self):
        graph = graph_fixture()
        voxel = mv.encode(graph)
        decoded = mv.decode(voxel, expected_sha256=voxel["sha256"])
        self.assertEqual(decoded, graph)
        self.assertEqual(mv.projection(decoded), mv.projection(graph))
        self.assertEqual(mv.source_bytes(decoded["sources"]["a"]), b"Source A\x00\xff")
        self.assertEqual(decoded["nodes"][1]["debt"], [{"kind": "unpaid", "quantity": 3}])

    def test_field_erasure_is_rejected(self):
        for field in ("contradictions", "debt", "phase", "address"):
            with self.subTest(field=field):
                graph = graph_fixture()
                del graph["nodes"][0][field]
                with self.assertRaises(ValueError):
                    mv.encode(graph)

    def test_shared_lifecycle_cannot_select_two_organs(self):
        graph = graph_fixture()
        graph["nodes"][1]["address"]["lifecycle_id"] = "build-1"
        with self.assertRaisesRegex(ValueError, "Multiple selected organs"):
            mv.encode(graph)

    def test_authority_never_crosses(self):
        for location in ("host", "edge", "residue"):
            graph = graph_fixture()
            if location == "host":
                graph["host"]["approval_inherited"] = True
            elif location == "edge":
                graph["edges"][0]["bridge"]["approval_binding"] = "old approval"
            else:
                graph["residue"]["executable"] = True
            with self.assertRaises(ValueError):
                mv.encode(graph)

    def test_identity_and_provenance_damage(self):
        changes = [("address", "body_id", "other"), ("address", "source_contract", "missing")]
        for group, key, value in changes:
            graph = graph_fixture()
            graph["nodes"][0][group][key] = value
            with self.assertRaises(ValueError):
                mv.encode(graph)
        graph = graph_fixture()
        graph["sources"]["a"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            mv.encode(graph)

    def test_dangling_edge_and_duplicate_node(self):
        graph = graph_fixture()
        graph["edges"][0]["target"] = "missing"
        with self.assertRaises(ValueError):
            mv.encode(graph)
        graph = graph_fixture()
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
        with self.assertRaises(ValueError):
            mv.encode(graph)

    def test_malformed_identifier_fails_as_value_error(self):
        for group, key in (("address", "source_contract"), ("address", "lifecycle_id")):
            graph = graph_fixture()
            graph["nodes"][0][group][key] = []
            with self.assertRaises(ValueError):
                mv.encode(graph)

    def test_expected_original_detects_valid_but_different_graph(self):
        graph = graph_fixture()
        original = mv.encode(graph)
        graph["nodes"][0]["phase"]["ordinal"] = 3
        changed = mv.encode(graph)
        with self.assertRaises(ValueError):
            mv.decode(changed, expected_sha256=original["sha256"])

    def test_compressed_corruption_truncation_trailing_and_expansion(self):
        original = mv.encode(graph_fixture())
        compressed = base64.b64decode(original["payload"])
        for data in (b"broken", compressed[:-1], compressed + b"trailing", zlib.compress(b"x" * 100000)):
            voxel = dict(original, payload=base64.b64encode(data).decode(), compressed_bytes=len(data))
            with self.assertRaises(ValueError):
                mv.decode(voxel)

    def test_projection_hash_is_checked(self):
        voxel = mv.encode(graph_fixture())
        voxel["projection_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            mv.decode(voxel)

    def test_json_and_path_bounds(self):
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
            with self.assertRaises(ValueError):
                mv.parse_json(data)
        for path in ("../x", "/x", "a//b", "a/./b", "a\\b"):
            with self.assertRaises(ValueError):
                mv.safe_path(path)

    def test_cycles_terminate_with_external_edges_and_debt(self):
        graph = graph_fixture()
        graph["edges"].append(yr.edge("b", "a", "depends_on", ["a"]))
        voxel = mv.encode(graph)
        tiny = mv.expand(voxel, "a", depth=0)
        self.assertEqual(tiny["external_node_ids"], ["b"])
        self.assertEqual(tiny["residue"]["debt"], [{"amount": 3}])
        self.assertEqual(len(mv.expand(voxel, "a", depth=16)["nodes"]), 2)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "lab"
        self.root.mkdir()
        fixture(self.root)

    def test_active_canon_and_round_trip_receipt(self):
        self.assertEqual(yr.check_canon(self.root)["status"], "ACTIVE")
        voxel = yr.snapshot(self.root)
        graph = mv.decode(voxel)
        self.assertEqual(graph["lineage"]["lifecycles"], 1)
        self.assertEqual(graph["lineage"]["verdict"], "PASS")
        report = yr.reenter(voxel, self.root)
        self.assertEqual(report["freshness"], "CURRENT")
        self.assertEqual(report["next_action"], "REVIEW_FINDINGS")
        self.assertEqual(report["authority"], "NONE")

    def test_source_change_requires_refresh(self):
        voxel = yr.snapshot(self.root)
        (self.root / "CURRENT_STATE.md").write_text("changed")
        self.assertEqual(yr.reenter(voxel, self.root)["next_action"], "REFRESH_SOURCES")

    def test_new_receipt_and_deleted_source_are_not_invisible(self):
        voxel = yr.snapshot(self.root)
        (self.root / "receipts/new.md").write_text("New evidence")
        self.assertEqual(yr.reenter(voxel, self.root)["freshness"], "STALE")
        (self.root / "receipts/new.md").unlink()
        (self.root / "src/lodestone_machinery/example.py").unlink()
        self.assertEqual(yr.reenter(voxel, self.root)["freshness"], "STALE")

    def test_changed_canon_fails_pack_and_reentry(self):
        voxel = yr.snapshot(self.root)
        (self.root / "node.md").write_text("Unadopted replacement")
        with self.assertRaisesRegex(ValueError, "Stale canon"):
            yr.snapshot(self.root)
        self.assertEqual(yr.reenter(voxel, self.root)["freshness"], "STALE")

    def test_symlink_and_large_source_fail_closed(self):
        target = self.root / "src/lodestone_machinery/example.py"
        target.unlink()
        target.symlink_to(self.root / "node.md")
        with self.assertRaises(ValueError):
            yr.snapshot(self.root)
        with self.assertRaises(ValueError):
            yr.read(self.root, "node.md", limit=2)

    def test_real_engine_records_all_consumed_and_missing_scope(self):
        workspace = Path(self.temp.name) / "organs"
        engine = Engine(workspace)
        samples = Path(__file__).resolve().parents[2] / "src/lodestone_machinery/samples"
        job = engine.submit(samples, ["workshop.md", "repair.md"], "ceramic")
        outcome = engine.run(job)
        self.assertEqual(outcome["state"], "COMPLETED")
        voxel = yr.snapshot(self.root, records_workspace=workspace)
        graph = mv.decode(voxel)
        records = [n for n in graph["nodes"] if n["role"].startswith("organ_record:")]
        self.assertGreaterEqual(len(records), 7)
        self.assertTrue(any(e["kind"] == "record_dependency" for e in graph["edges"]))
        self.assertEqual(yr.reenter(voxel, self.root)["freshness"], "UNAVAILABLE")
        self.assertEqual(yr.reenter(voxel, self.root, records_workspace=workspace)["freshness"], "CURRENT")
        engine.records.publish("ruler", "comparison", {"new": True})
        self.assertEqual(yr.reenter(voxel, self.root, records_workspace=workspace)["freshness"], "STALE")

    def test_damaged_record_is_rejected(self):
        store = RecordStore(Path(self.temp.name) / "records")
        handle = store.publish("ruler", "comparison", {"observed": 2})
        store.path(handle).write_text('{"schema":"wrong"}')
        with self.assertRaises(ValueError):
            yr.snapshot(self.root, records_workspace=store.workspace)

    def test_origami_claims_relations_unknowns_and_usage_survive(self):
        result = {"schema_version": "0.1.0", "run_id": "fixture", "usage": {"tokens": None},
                  "fold": {"claims": [{"id": "a", "basis": "RECORDED", "value": 1},
                                      {"id": "b", "basis": "UNKNOWN", "value": 2}],
                           "relations": [{"from": "a", "to": "b", "type": "contradicts"}],
                           "unresolved": ["conflicting accounts"]}}
        path = Path(self.temp.name) / "result.json"
        path.write_text(json.dumps(result))
        voxel = yr.snapshot(self.root, origami_results=[path])
        graph = mv.decode(voxel)
        self.assertEqual(next(n["state"] for n in graph["nodes"] if n["role"] == "origami_result"), result)
        self.assertTrue(any(e["kind"] == "origami:contradicts" for e in graph["edges"]))
        self.assertEqual(yr.reenter(voxel, self.root)["freshness"], "UNAVAILABLE")
        self.assertEqual(yr.reenter(voxel, self.root, origami_results=[path])["freshness"], "CURRENT")

    def test_new_process_pack_expand_reenter_and_stale_exit(self):
        runner = Path(__file__).resolve().parents[2] / "tools/machinery/run"
        output = Path(self.temp.name) / "voxel.json"

        def call(*args):
            return subprocess.run([sys.executable, "-B", str(runner), "yggdrasil", *map(str, args)],
                                  capture_output=True, text=True, timeout=30)

        packed = call("pack", "--root", self.root, "--output", output)
        self.assertEqual(packed.returncode, 0, packed.stderr)
        expanded = call("expand", output, "--anchor", "repo:node.md", "--depth", 0)
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertEqual(json.loads(expanded.stdout)["nodes"][0]["id"], "repo:node.md")
        self.assertEqual(call("reenter", output, "--root", self.root).returncode, 0)
        (self.root / "CURRENT_STATE.md").write_text("changed")
        self.assertEqual(call("reenter", output, "--root", self.root).returncode, 2)
        self.assertEqual(call("pack", "--root", self.root, "--output", self.root / "bad.json").returncode, 2)

    def test_actual_origami_saved_result_is_consumed(self):
        organ = Origami(Path(self.temp.name) / "origami")
        run_id = organ.start(self.root, ["node.md"], "What is recorded?", "fixture",
                             "test author: bounded read only",
                             roles={"constructor": "a", "consumer": "b", "checker": "c", "provider": "a"})["run_id"]
        request = organ.request(run_id)
        response = {"request_id": request["request_id"], "decision": "UNRESOLVED", "claims": [],
                    "relations": [], "alternatives": [], "answer": None, "answer_claim_ids": [],
                    "next_check": None, "coverage_assumptions": ["Fixture source only"], "unresolved": ["Need review"]}
        result = organ.advance(run_id, {"provider": "scripted fixture", "model": "NONE",
                                       "response_id": None, "usage": None, "response": response})
        self.assertEqual(result["status"], "UNRESOLVED", result.get("reason"))
        saved = Path(result["artifact"]).with_name("result.json")
        voxel = yr.snapshot(self.root, origami_results=[saved])
        graph = mv.decode(voxel)
        self.assertEqual(next(n["state"] for n in graph["nodes"] if n["role"] == "origami_result"),
                         json.loads(saved.read_text()))
        self.assertTrue(any(e["kind"] == "origami_source" for e in graph["edges"]))
        self.assertEqual(yr.reenter(voxel, self.root, origami_results=[saved])["next_action"], "REVIEW_FINDINGS")

    def test_failed_origami_result_is_retained_without_inventing_fold(self):
        result = {"schema_version": "0.1.0", "status": "PROVIDER_FAILED", "fold": None,
                  "reason": "Provider failed", "usage": {"total_tokens": None}}
        saved = Path(self.temp.name) / "failed.json"
        saved.write_text(json.dumps(result))
        voxel = yr.snapshot(self.root, origami_results=[saved])
        graph = mv.decode(voxel)
        recorded = next(n for n in graph["nodes"] if n["role"] == "origami_result")
        self.assertEqual(recorded["state"], result)
        self.assertTrue(recorded["unknowns"])


if __name__ == "__main__":
    unittest.main()

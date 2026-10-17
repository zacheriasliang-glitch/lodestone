"""Focused checks for the Origami re-entry fold tool.

Synthetic fixtures live in temporary directories. No original evidence is modified.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
sys.path.insert(0, str(TOOL_ROOT / "src"))

import origami_fold as of  # noqa: E402

TOOL_REL = "tools/origami-fold/src/origami_fold.py"
DIRECTIVE = "do the thing\nexactly"
DIRECTIVE_SHA = of.sha256_bytes(DIRECTIVE.encode("utf-8"))


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def receipt_text(operation: str, prompt: str | None = None) -> str:
    body = f"# receipt\n\nreceipt_id: RCP-x\ntimestamp: t\noperation_name: {operation}\nmode: write\n\n"
    if prompt is not None:
        body += "## initiating_prompt\n\n```text\n" + prompt + "\n```\n\n"
    body += "## objective\n\nx\n"
    return body


def make_synthetic(root: Path) -> Path:
    write(root, "CURRENT_STATE.md", "# State\n\n- STATUS = CLOSED\n- next: review\n")
    write(root, "RECORD_A.md", "# Record A\n\nHost accepts the finding.\nVerdict: CONDITIONAL\n")
    write(root, "receipts/20260901T000000Z__op-a__RCP-20260901T000000Z-aaaaaaaaaaaa.md", receipt_text("op-a"))
    shutil.copy(REPOSITORY_ROOT / TOOL_REL, root / TOOL_REL) if (root / TOOL_REL).parent.mkdir(parents=True, exist_ok=True) is None else None
    data_c = (root / "CURRENT_STATE.md").read_bytes()
    data_a = (root / "RECORD_A.md").read_bytes()
    sources = [
        of.bind_source(root, "C", "CURRENT_STATE.md", record_kind="checkpoint"),
        of.bind_source(root, "A", "RECORD_A.md", record_kind="record"),
        {"id": "HD", "path": None, "resolution": {"operation_name": "op-fold"},
         "bound": {"sha256": DIRECTIVE_SHA, "byte_count": len(DIRECTIVE)}},
    ]
    built_at = "2026-09-02T00:00:00Z"
    fold = {
        "schema": {"name": of.SCHEMA_NAME, "version": of.SCHEMA_VERSION},
        "work_item": {"id": "wi", "title": "synthetic", "question": "q?",
                      "epoch": {"description": "synthetic"}, "next_permitted_action": "none"},
        "construction": {"built_at_utc": built_at, "built_by": "test", "operation_name": "op-fold",
                         "authorization": {"title": "synthetic directive", "directive_sha256": DIRECTIVE_SHA},
                         "interpretation_author": "test", "not_this": ["anything real"],
                         "telemetry": {"tokens": "UNKNOWN", "cost": "UNKNOWN"}},
        "recipe": {"version": "1.0.0", "files": [{"path": TOOL_REL, "sha256": of.sha256_bytes((root / TOOL_REL).read_bytes())}]},
        "sources": sources,
        "claims": [
            {"id": "a.accepts", "status": "ESTABLISHED", "layer": "EX", "basis": "recorded",
             "scope": {"kind": "current", "epoch": "e"}, "text": "Host accepts the finding.",
             "support": [of.bind_support(data_a, "A", 3, 3, "Host accepts the finding.")], "refresh_when": []},
            {"id": "a.verdict", "status": "ESTABLISHED", "layer": "EX", "basis": "recorded",
             "scope": {"kind": "historical", "epoch": "e"}, "text": "Verdict CONDITIONAL.",
             "support": [of.bind_support(data_a, "A", 4, 4, "Verdict: CONDITIONAL")], "refresh_when": []},
            {"id": "s.status", "status": "ESTABLISHED", "layer": "EX", "basis": "recorded",
             "scope": {"kind": "current", "epoch": "e"}, "text": "STATUS = CLOSED.",
             "support": [of.bind_support(data_c, "C", 3, 3, "STATUS = CLOSED")],
             "refresh_when": ["state_index_changed", "new_receipt"]},
            {"id": "interp.x", "status": "ESTABLISHED", "layer": "AP", "basis": "interpretation",
             "scope": {"kind": "current", "epoch": "e"}, "text": "The acceptance closes the matter (reading).",
             "support": [of.bind_support(data_a, "A", 3, 3, "Host accepts")], "refresh_when": []},
            {"id": "hd.auth", "status": "ESTABLISHED", "layer": "EX", "basis": "recorded",
             "scope": {"kind": "current", "epoch": "e"}, "text": "authorized",
             "support": [{"source": "HD", "lines": None, "quote": "do the thing"}], "refresh_when": []},
        ],
        "relations": [
            {"id": "rel.a.supersedes", "type": "supersedes", "from": "a.accepts", "to": "a.verdict",
             "basis": "recorded", "note": "n", "support": [of.bind_support(data_a, "A", 3, 3, None)]},
        ],
        "open_items": [
            {"id": "open.k", "kind": "HOST_INPUT_REQUIRED", "basis": "recorded", "text": "review",
             "support": [of.bind_support(data_c, "C", 4, 4, "next: review")], "refresh_when": ["state_index_changed"]},
        ],
        "index_dependencies": {
            "state_index": {"source": "C"},
            "receipts": {"directory": "receipts", "cutoff_basic_utc": of.utc_basic(built_at),
                         "bound": {"count": 1, "newest": "receipts/20260901T000000Z__op-a__RCP-20260901T000000Z-aaaaaaaaaaaa.md",
                                   "listing_sha256": of.listing_sha256(of.receipt_files(root))}},
            "host_dispositions": {"patterns": ["HOST_DISPOSITION__*.md"],
                                  "bound": {"names": [], "listing_sha256": of.listing_sha256([])}},
            "origami_records": {"patterns": ["ORIGAMI__*.md"],
                                "bound": {"names": [], "listing_sha256": of.listing_sha256([])}},
            "head": {"bound": "UNKNOWN", "role": "provenance only"},
        },
        "acknowledged_receipts": [],
    }
    fold_path = root / "tools/origami-fold/folds/synthetic/fold.json"
    of.save_fold(fold_path, fold)
    return fold_path


class LinePartitionTests(unittest.TestCase):
    def test_line_bytes_v1_semantics(self):
        self.assertEqual(of.segment_lines(b"a\nb\n"), [b"a\n", b"b\n"])
        self.assertEqual(of.segment_lines(b"a\nb"), [b"a\n", b"b"])
        self.assertEqual(of.segment_lines(b""), [])
        payload, start, end = of.slice_lines(b"ab\ncd\nef\n", 2, 3)
        self.assertEqual((payload, start, end), (b"cd\nef\n", 3, 9))
        with self.assertRaises(of.FoldError):
            of.slice_lines(b"ab\n", 2, 2)
        self.assertEqual(of.lines_covering(b"ab\ncd\nef\n", 4, 8), (2, 3))


class SyntheticFoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fold_path = make_synthetic(self.root)
        self.fold = of.load_fold(self.fold_path)

    def tearDown(self):
        self.tmp.cleanup()

    def statuses(self, fold=None):
        report = of.check_fold(self.root, fold or self.fold)
        return {key: value["status"] for key, value in report["entries"].items()}, report

    def test_saved_state_loads_verifies_and_orients_fresh(self):
        self.assertEqual(of.verify(self.root, self.fold), [])
        statuses, report = self.statuses()
        self.assertEqual(set(statuses.values()), {of.FRESH})
        text = of.orient_text(self.fold, report)
        for entry_id in ("a.accepts", "a.verdict", "s.status", "open.k"):
            self.assertIn(entry_id, text)
        self.assertIn("A:L3-3", text)
        self.assertEqual(report["by_receipt"]["HD"]["status"], "NOT_FOUND")

    def test_expand_returns_exact_passage_with_identity(self):
        text, code = of.expand_text(self.root, self.fold, "A:L3-4")
        self.assertEqual(code, 0)
        self.assertIn("Host accepts the finding.\nVerdict: CONDITIONAL", text)
        self.assertIn("[MATCH]", text)
        text, code = of.expand_text(self.root, self.fold, "a.verdict")
        self.assertEqual(code, 0)
        self.assertIn("quote present: yes", text)
        self.assertIn("[INTACT]", text)
        self.assertNotIn("Host accepts", text.split("--- passage")[1])
        with self.assertRaises(of.FoldError):
            of.expand_text(self.root, self.fold, "nope")

    def test_missing_source_is_explicit_and_others_preserved(self):
        (self.root / "RECORD_A.md").unlink()
        statuses, report = self.statuses()
        self.assertEqual(statuses["a.accepts"], of.MISSING_SOURCE)
        self.assertEqual(statuses["a.verdict"], of.MISSING_SOURCE)
        self.assertEqual(statuses["rel.a.supersedes"], of.MISSING_SOURCE)
        self.assertEqual(statuses["s.status"], of.FRESH)
        self.assertEqual(statuses["open.k"], of.FRESH)
        self.assertIn("source_missing:A", report["entries"]["a.accepts"]["reasons"])
        text, code = of.expand_text(self.root, self.fold, "a.accepts")
        self.assertEqual(code, 1)
        self.assertIn("MISSING", text)
        self.assertNotIn("--- passage", text)
        self.assertTrue(any("MISSING" in error for error in of.verify(self.root, self.fold)))

    def test_changed_passage_flags_only_affected_entries(self):
        write(self.root, "RECORD_A.md", "# Record A\n\nHost accepts the finding.\nVerdict: PASS\n")
        statuses, report = self.statuses()
        self.assertEqual(statuses["a.verdict"], of.NEEDS_REVIEW)
        self.assertIn("passage_changed:A:L4-4", report["entries"]["a.verdict"]["reasons"])
        self.assertEqual(statuses["a.accepts"], of.FRESH)
        self.assertIn("source_changed_passage_intact:A", report["entries"]["a.accepts"]["notes"])
        self.assertEqual(statuses["s.status"], of.FRESH)
        self.assertTrue(report["sources"]["A"]["changed"])
        text, code = of.expand_text(self.root, self.fold, "a.verdict")
        self.assertEqual(code, 1)
        self.assertIn("CURRENT BYTES DIFFER FROM BOUND", text)
        self.assertIn("Verdict: PASS", text)

    def test_moved_passage_reports_relocation_candidate_without_rebinding(self):
        write(self.root, "RECORD_A.md", "# Record A\n\ninserted line\nHost accepts the finding.\nVerdict: CONDITIONAL\n")
        statuses, report = self.statuses()
        self.assertEqual(statuses["a.accepts"], of.NEEDS_REVIEW)
        self.assertIn("relocation_candidate:A:L4-4", report["entries"]["a.accepts"]["notes"])
        self.assertEqual(self.fold["claims"][0]["support"][0]["lines"], [3, 3])

    def test_state_index_change_flags_declared_entries_only(self):
        with (self.root / "CURRENT_STATE.md").open("a", encoding="utf-8") as handle:
            handle.write("- later note\n")
        statuses, report = self.statuses()
        self.assertTrue(report["triggers_fired"]["state_index_changed"])
        self.assertEqual(statuses["s.status"], of.NEEDS_REVIEW)
        self.assertEqual(statuses["open.k"], of.NEEDS_REVIEW)
        self.assertEqual(statuses["a.accepts"], of.FRESH)
        self.assertEqual(statuses["a.verdict"], of.FRESH)

    def test_new_receipt_listed_and_construction_receipt_resolves_directive(self):
        write(self.root, "receipts/20260903T000000Z__op-later__RCP-20260903T000000Z-bbbbbbbbbbbb.md", receipt_text("op-later"))
        statuses, report = self.statuses()
        self.assertEqual([item["path"] for item in report["index"]["receipts"]["unfolded"]],
                         ["receipts/20260903T000000Z__op-later__RCP-20260903T000000Z-bbbbbbbbbbbb.md"])
        self.assertEqual(statuses["s.status"], of.NEEDS_REVIEW)
        self.assertEqual(statuses["a.verdict"], of.FRESH)
        write(self.root, "receipts/20260904T000000Z__op-fold__RCP-20260904T000000Z-cccccccccccc.md",
              receipt_text("op-fold", DIRECTIVE))
        statuses, report = self.statuses()
        self.assertEqual(report["index"]["receipts"]["construction_receipt"],
                         "receipts/20260904T000000Z__op-fold__RCP-20260904T000000Z-cccccccccccc.md")
        self.assertEqual(len(report["index"]["receipts"]["unfolded"]), 1)
        self.assertEqual(report["by_receipt"]["HD"], {**report["by_receipt"]["HD"], "status": "RESOLVED", "match": True})
        text, code = of.expand_text(self.root, self.fold, "hd.auth")
        self.assertEqual(code, 0)
        self.assertIn("[MATCH]", text)
        self.assertIn(DIRECTIVE, text)
        acknowledged = of.rebind(self.root, self.fold,
                                 receipt="receipts/20260903T000000Z__op-later__RCP-20260903T000000Z-bbbbbbbbbbbb.md",
                                 reviewed_by="tester", note="does not affect the fold")
        statuses, report = self.statuses(acknowledged)
        self.assertEqual(report["index"]["receipts"]["unfolded"], [])
        self.assertEqual(statuses["s.status"], of.FRESH)

    def test_refresh_apply_updates_only_derived_entries(self):
        write(self.root, "RECORD_A.md", "# Record A\n\nHost accepts the finding.\nVerdict: PASS\n")
        report = of.check_fold(self.root, self.fold)
        updated = of.apply_report(self.fold, report)
        for original, new in zip(of.all_entries(self.fold), of.all_entries(updated)):
            self.assertEqual(original["support"], new["support"])
            self.assertEqual(original.get("text"), new.get("text"))
        self.assertEqual(updated["claims"][1]["freshness"]["status"], of.NEEDS_REVIEW)
        self.assertEqual(updated["claims"][0]["freshness"]["status"], of.FRESH)
        self.assertEqual(updated["sources"][1]["bound"], self.fold["sources"][1]["bound"])
        self.assertTrue(updated["sources"][1]["observed"]["changed"])
        self.assertEqual(len(updated["refresh_history"]), 1)
        of.save_fold(self.fold_path, updated)
        self.fold_path.with_name("fold.md").write_text(of.render_markdown(updated), encoding="utf-8")
        self.assertIn("NEEDS_REVIEW (passage_changed:A:L4-4)", self.fold_path.with_name("fold.md").read_text(encoding="utf-8"))

    def test_rebind_refuses_unreviewed_locator_then_restores_fresh(self):
        write(self.root, "RECORD_A.md", "# Record A\n\nHost accepts the finding.\nVerdict: PASS\n")
        with self.assertRaises(of.FoldError):
            of.rebind(self.root, self.fold, source_id="A", reviewed_by="tester")
        self.fold["claims"][1]["support"][0]["quote"] = "Verdict: PASS"
        self.fold["claims"][1]["text"] = "Verdict PASS (reviewed)."
        unreviewed = of.rebind(self.root, self.fold, source_id="A")
        statuses, _ = self.statuses(unreviewed)
        self.assertEqual(statuses["a.verdict"], of.FRESH)
        self.assertEqual(statuses["interp.x"], of.NEEDS_REVIEW)
        reviewed = of.rebind(self.root, self.fold, source_id="A", reviewed_by="tester", note="checked")
        statuses, _ = self.statuses(reviewed)
        self.assertEqual(set(statuses.values()), {of.FRESH})
        self.assertEqual(of.verify(self.root, reviewed), [])

    def test_recipe_change_flags_every_entry(self):
        with (self.root / TOOL_REL).open("a", encoding="utf-8") as handle:
            handle.write("# touched\n")
        statuses, report = self.statuses()
        self.assertTrue(report["recipe"]["changed"])
        self.assertEqual(set(statuses.values()), {of.NEEDS_REVIEW})
        rebound = of.rebind(self.root, self.fold, recipe=True, reviewed_by="tester")
        statuses, _ = self.statuses(rebound)
        self.assertEqual(set(statuses.values()), {of.FRESH})

    def test_render_is_deterministic(self):
        first = of.render_markdown(self.fold)
        second = of.render_markdown(self.fold)
        self.assertEqual(first, second)
        self.assertIn("## Established", first)
        self.assertIn("`A:L3-3`", first.replace("A:L3-3", "`A:L3-3`"))

    def test_cli_in_new_process(self):
        tool = self.root / TOOL_REL
        base = [sys.executable, str(tool), "--root", str(self.root), "--fold", str(self.fold_path)]
        result = subprocess.run(base + ["orient"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ORIGAMI FOLD — orient", result.stdout)
        result = subprocess.run(base + ["expand", "A:L4-4"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verdict: CONDITIONAL", result.stdout)
        result = subprocess.run(base + ["refresh"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stdout)
        self.assertFalse(self.fold_path.with_name("fold.md").exists())



if __name__ == "__main__":
    unittest.main()

"""One-candidate append safety, including random-ID same-second join ordering."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "tools/machinery/append_receipt.py"
spec = importlib.util.spec_from_file_location("append_receipt", SCRIPT)
append = importlib.util.module_from_spec(spec)
spec.loader.exec_module(append)


class AppendReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "repo"
        self.root.mkdir()
        (self.root / "receipts").mkdir()
        (self.root / "source.txt").write_text("A preserved source.\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "source.txt"], check=True)

    def historical(self, stamp, suffix, paths=None, join=None, mode="write", slug="historical"):
        rid = "RCP-" + stamp + "-" + suffix
        path = self.root / "receipts" / f"{stamp}__{slug}__{rid}.md"
        doc = {"receipt_id": rid, "mode": mode}
        if paths is not None:
            doc[append.lineage.PATH_SET_KEY] = paths
        if join:
            doc[append.lineage.JOIN_PREDECESSORS_KEY] = join[0]
            doc[append.lineage.JOIN_SETS_KEY] = join[1]
        path.write_text("# Historical receipt\n\n```json\n" + json.dumps(doc) + "\n```\n")
        return path.relative_to(self.root).as_posix()

    def draft(self, stamp="20260105T010000Z", suffix="123456789abc", updates=None, slug="new-operation"):
        rid = "RCP-" + stamp + "-" + suffix
        doc = {key: "Recorded observation" for key in append.REQUIRED}
        doc.update(receipt_id=rid, timestamp=append.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").isoformat()+"Z",
                   operation_name=slug, mode="verification", completion_status="COMPLETE",
                   deterministic_snapshot_hash=append.snapshot_hash(self.root))
        for key in ("files_read", "files_created", "files_modified", "files_deleted", "warnings", "unresolved_questions"):
            doc[key] = []
        doc[append.lineage.PATH_SET_KEY] = sorted(append._corpus(self.root))
        if updates:
            doc.update(updates)
        path = self.base / f"{stamp}__{slug}__{rid}.md"
        self.write_draft(path, doc)
        return path, doc

    @staticmethod
    def write_draft(path, doc):
        path.write_text("# Exact draft é\n\n```json\n" + json.dumps(doc, ensure_ascii=False, indent=2) + "\n```\n")

    def refused(self, draft, message):
        before = append._corpus(self.root)
        with self.assertRaisesRegex(ValueError, message):
            append.process(self.root, draft, append=True)
        self.assertEqual(append._corpus(self.root), before)
        self.assertFalse((self.root / "receipts" / draft.name).exists())

    def test_default_preflight_and_exact_ordinary_append_preserve_historical_findings(self):
        old = self.historical("20260101T000000Z", "000000000001", paths=[], mode="UNKNOWN")
        before_bytes = (self.root / old).read_bytes()
        before = append.lineage.check(self.root)
        draft, _ = self.draft()
        exact = draft.read_bytes()
        report = append.process(self.root, draft)
        self.assertEqual(report["status"], "PREFLIGHT_OK")
        self.assertFalse((self.root / "receipts" / draft.name).exists())
        report = append.process(self.root, draft, append=True)
        self.assertEqual(report["status"], "APPENDED")
        self.assertEqual((self.root / report["target"]).read_bytes(), exact)
        self.assertEqual((self.root / old).read_bytes(), before_bytes)
        self.assertEqual(append.lineage.check(self.root)["findings"], before["findings"])
        self.assertEqual(report["heads_before"], report["heads_after"])

    def test_same_timestamp_dependent_join_is_refused_without_a_receipt_write(self):
        first = self.historical("20260101T000000Z", "000000000001", paths=[])
        left = self.historical("20260102T000000Z", "000000000001", paths=[first])
        right = self.historical("20260103T000000Z", "000000000001", paths=[first])
        parent = self.historical("20260104T000000Z", "ffffffffffff", join=([left, right], [[first, left], [first, right]]))
        self.assertEqual(append.lineage.check(self.root)["verdict"], "PASS")
        draft, doc = self.draft("20260104T000000Z", "000000000002")
        doc[append.OBSERVED_PATHS] = doc.pop(append.lineage.PATH_SET_KEY)
        doc[append.lineage.JOIN_PREDECESSORS_KEY] = [parent, right]
        doc[append.lineage.JOIN_SETS_KEY] = [[first, left, right, parent], [first, right]]
        self.write_draft(draft, doc)
        self.refused(draft, "new lineage findings.*not a preceding lifecycle")

    def test_duplicate_name_and_duplicate_id_do_not_overwrite(self):
        draft, doc = self.draft()
        append.process(self.root, draft, append=True)
        before = append._corpus(self.root)
        with self.assertRaisesRegex(ValueError, "already exists"):
            append.process(self.root, draft, append=True)
        other = self.base / draft.name.replace("__new-operation__", "__different-slug__")
        self.write_draft(other, doc)
        with self.assertRaisesRegex(ValueError, "already exists"):
            append.process(self.root, other, append=True)
        self.assertEqual(append._corpus(self.root), before)

    def test_existing_target_symlink_is_never_followed(self):
        draft, _ = self.draft()
        other = self.base / "keep.txt"
        other.write_bytes(b"untouched")
        target = self.root / "receipts" / draft.name
        target.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            append.process(self.root, draft, append=True)
        self.assertTrue(target.is_symlink())
        self.assertEqual(other.read_bytes(), b"untouched")

    def test_internal_and_symlink_drafts_are_refused(self):
        draft, _ = self.draft()
        internal = self.root / draft.name
        internal.write_bytes(draft.read_bytes())
        self.refused(internal, "external regular file")
        linked = self.base / "linked.md"
        linked.symlink_to(draft)
        self.refused(linked, "external regular file")

    def test_noncanonical_name_and_mismatched_id_or_time_are_refused(self):
        draft, doc = self.draft()
        invalid = self.base / draft.name.replace("123456789abc", "123456789abC")
        invalid.write_bytes(draft.read_bytes())
        self.refused(invalid, "canonical")
        for update, message in (({"receipt_id": "RCP-20261003T010000Z-000000000001"}, "does not match"),
                                ({"timestamp": "2026-10-03T02:00:00Z"}, "timestamp")):
            with self.subTest(update=update):
                self.write_draft(draft, {**doc, **update})
                self.refused(draft, message)

    def test_future_timestamp_is_refused_before_receipt_write(self):
        draft, _ = self.draft("29990101T000000Z")
        self.refused(draft, "must not be in the future")

    def test_each_required_field_and_snapshot_are_checked(self):
        draft, doc = self.draft()
        for key in append.REQUIRED:
            with self.subTest(missing=key):
                incomplete = dict(doc)
                del incomplete[key]
                self.write_draft(draft, incomplete)
                self.refused(draft, "missing required")
        self.write_draft(draft, {**doc, "deterministic_snapshot_hash": "0" * 64})
        self.refused(draft, "snapshot hash")
        self.write_draft(draft, {**doc, "mode": "implementation"})
        self.refused(draft, "invalid mode")

    def test_undeclared_custody_and_stale_observed_paths_are_refused(self):
        draft, doc = self.draft()
        del doc[append.lineage.PATH_SET_KEY]
        self.write_draft(draft, doc)
        self.refused(draft, "observed path set")
        draft, _ = self.draft()
        self.historical("20260101T000000Z", "000000000001", paths=[])
        self.refused(draft, "observed path set")

    def test_mutated_draft_and_stale_corpus_during_validation_are_refused(self):
        old = self.historical("20260101T000000Z", "000000000001", paths=[])
        check_delta = append._delta
        for subject in ("draft", "corpus", "snapshot"):
            with self.subTest(subject=subject):
                draft, _ = self.draft()
                def mutate(before, after):
                    check_delta(before, after)
                    path = {"draft": draft, "corpus": self.root / old, "snapshot": self.root / "source.txt"}[subject]
                    with path.open("ab") as stream:
                        stream.write(b"\nchanged\n")
                with mock.patch.object(append, "_delta", side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, "changed during preflight"):
                        append.process(self.root, draft, append=True)
                self.assertFalse((self.root / "receipts" / draft.name).exists())

    def test_equal_finding_count_cannot_hide_a_different_finding(self):
        before = {"findings": [{"law": "Y3_CLOSED_PREFIX", "receipt": "old", "detail": "old defect"}], "heads": ["a"], "lifecycles": 1}
        after = {**before, "findings": [{"law": "Y3_CLOSED_PREFIX", "receipt": "new", "detail": "different defect"}]}
        with self.assertRaisesRegex(ValueError, "new lineage findings"):
            append._delta(before, after)

    def test_head_increase_is_refused_even_with_no_new_nonhead_findings(self):
        before = {"findings": [], "heads": ["a"], "lifecycles": 1}
        after = {"findings": [{"law": "Y4_ONE_HEAD", "receipt": "", "detail": "2 heads"}], "heads": ["a", "b"], "lifecycles": 2}
        with self.assertRaisesRegex(ValueError, "increase lineage head"):
            append._delta(before, after)

    def test_cli_defaults_to_preflight_and_snapshot_uses_git_visible_files(self):
        (self.root / "untracked.txt").write_bytes(b"included")
        (self.root / "alias.txt").symlink_to("source.txt")
        draft, _ = self.draft()
        done = subprocess.run([sys.executable, "-B", str(SCRIPT), "--root", str(self.root), "--draft", str(draft)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "PREFLIGHT_OK")
        self.assertFalse((self.root / "receipts" / draft.name).exists())
        original = append.snapshot_hash(self.root)
        (self.root / "untracked.txt").write_bytes(b"changed")
        self.assertNotEqual(append.snapshot_hash(self.root), original)


if __name__ == "__main__":
    unittest.main()

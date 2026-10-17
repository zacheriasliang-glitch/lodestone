"""The Yggdrasil adapter: one law<TAB>subject line per finding, nothing else, matched exactly by the registry gate."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "tools/machinery/yggdrasil_findings.py"
GATE = ROOT / "tools/machinery/check_bezoar.py"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load(ADAPTER)

REPORT = {"verdict": "VIOLATION", "findings": [
    {"law": "Y2_ONE_ORGAN", "receipt": "receipts/b.md", "detail": "mode outside the policy set"},
    {"law": "IDENTITY", "receipt": "receipts/b.md", "detail": "receipt id is malformed"},
    {"law": "Y3_CLOSED_PREFIX", "receipt": "receipts/c.md", "detail": "omits receipts/x.md"},
    {"law": "Y3_CLOSED_PREFIX", "receipt": "receipts/c.md", "detail": "omits receipts/y.md"},
    {"law": "IDENTITY", "receipt": "receipts/a.md", "detail": "receipt id is malformed"},
    {"law": "Y4_ONE_HEAD", "receipt": "", "detail": "declared subgraph presents 2 heads"},
]}


class AdapterTests(unittest.TestCase):
    def test_one_line_per_finding_sorted_by_law_then_subject_with_no_prose(self):
        lines = adapter.lines_from_report(REPORT)
        self.assertEqual(lines, ["IDENTITY\treceipts/a.md", "IDENTITY\treceipts/b.md",
                                 "Y2_ONE_ORGAN\treceipts/b.md",
                                 "Y3_CLOSED_PREFIX\treceipts/c.md", "Y3_CLOSED_PREFIX\treceipts/c.md",
                                 "Y4_ONE_HEAD\treceipts/"])
        for line in lines:
            self.assertEqual(line.count("\t"), 1)
            self.assertNotIn("detail", line)

    def test_finding_naming_no_receipt_is_the_declared_subgraph(self):
        self.assertEqual(adapter.subject_of({"law": "Y4_ONE_HEAD", "receipt": ""}), "receipts/")
        self.assertEqual(adapter.subject_of({"law": "Y4_ONE_HEAD", "receipt": "  "}), "receipts/")

    def test_subjects_the_registry_cannot_match_exactly_are_refused(self):
        for bad in ("receipts/a,b.md", "receipts/a\tb.md", "receipts/a\nb.md"):
            with self.assertRaises(ValueError):
                adapter.subject_of({"law": "IDENTITY", "receipt": bad})
        with self.assertRaises(ValueError):
            adapter.lines_from_report({"verdict": "PASS"})
        with self.assertRaises(ValueError):
            adapter.lines_from_report({"findings": [{"law": "", "receipt": "receipts/a.md"}]})

    def test_saved_report_and_gate_agree_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text(json.dumps(REPORT))
            done = subprocess.run([sys.executable, "-B", str(ADAPTER), "--report", str(report)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.splitlines(), adapter.lines_from_report(REPORT))
            findings = Path(temporary) / "findings.tsv"
            findings.write_text(done.stdout)
            registry = Path(temporary) / "bezoar.md"
            registry.write_text("# registry\n\n## BZ-0001\n\n    kind: SEED\n    law: IDENTITY\n"
                                "    subject: receipts/a.md, receipts/b.md\n    first_seen: abc1234\n"
                                "    state: HELD\n    clears_when: ids repaired\n\nTwo malformed ids, one cause.\n")
            gate = subprocess.run([sys.executable, "-B", str(GATE), str(registry), "--findings", str(findings)],
                                  capture_output=True, text=True)
            self.assertEqual(gate.returncode, 2)
            self.assertNotIn("IDENTITY on receipts/a.md", gate.stdout)
            self.assertIn("Y2_ONE_ORGAN on receipts/b.md", gate.stdout)
            self.assertIn("Y4_ONE_HEAD on receipts/", gate.stdout)

    def test_fixture_corpus_emits_exactly_the_detector_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            (fixture / "receipts").mkdir()
            (fixture / "receipts/20260101T000000Z__fixture__RCP-20260101T000000Z-000000000001.md").write_text(
                '# Fixture\n\n```json\n' + json.dumps({"receipt_id": "RCP-20260101T000000Z-000000000001", "mode": "UNKNOWN"}) + '\n```\n')
            done = subprocess.run([sys.executable, "-B", str(ADAPTER), "--root", str(fixture)],
                                  capture_output=True, text=True, timeout=600)
            self.assertEqual(done.returncode, 0, done.stderr)
            lines = done.stdout.splitlines()
            report = json.loads(subprocess.run([sys.executable, "-B", str(ROOT / "tools/machinery/run"), "yggdrasil",
                                                "--root", str(fixture), "--findings"], capture_output=True, text=True).stdout)
            self.assertEqual(len(lines), len(report["findings"]))
            self.assertEqual(lines, sorted(lines))
            for line in lines:
                law, subject = line.split("\t")
                self.assertIn(law, report["counts"])
                self.assertTrue(subject == "receipts/" or subject.startswith("receipts/2"), subject)

if __name__ == "__main__":
    unittest.main()

"""Human-curated handoff lifecycle on each native OS; no model or network calls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("LODESTONE_DESKTOP_TEST_RUNTIME", ROOT / "desktop/runtime")).resolve()
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery.loom_handoff import LoomHandoff
from lodestone_machinery import loom_handoff as handoff, origami
from lodestone_machinery.organs.sources import anchor
from lodestone_nurarihyon import portable_files

spec = importlib.util.spec_from_file_location("handoff_bootstrap", ROOT / "desktop/bootstrap_learning.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class CuratedHandoff(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "sources λ"
        self.source.mkdir()
        (self.source / "note.txt").write_text(
            "Only when a completed action has an observed result may this fixture report success.\n",
            encoding="utf-8")
        self.memory = LearningMemory(self.root / "memory")
        self.seeds = bootstrap._bootstrap(self.memory)["seed_handles"]
        self.loom = UnifiedLoom(self.memory, self.seeds)
        self.original = self.loom.run(encounter="What must precede a success claim?",
            purpose="Exercise an authored human-curation fixture.", reader="scripted test provider",
            provider=lambda packet: {"status": "FAILED", "response": "Authored incomplete answer",
                                     "observation": {"model_called": False}})
        self.correction = "Check the observed result before claiming completion."
        self.loom.correct(self.original["outcome_handle"], author="Fixture reviewer",
            correction=self.correction, observation="The authored answer omitted its condition.",
            event_id="curated-handoff-test")
        self.workspace = self.root / "handoffs λ"
        self.handoff = LoomHandoff(self.loom, self.workspace)

    def preview(self):
        return self.loom.preview_origami(self.original["outcome_handle"], workspace=self.workspace,
                                         root=self.source, paths=["note.txt"])

    @staticmethod
    def curation(proposal, decision="ANSWER"):
        return {"curator": "Fixture human curator", "checker": "Fixture independent checker",
                "decision": decision, "answer": "Observe the actual result first.",
                "relationship": "A success claim depends on an observed result.",
                "conditions": "An action has actually completed and its result is available.",
                "action": "Inspect the actual result.", "effect": "A claim can be checked.",
                "uncertainty": "No other sources or live user events were examined.",
                "limits": "Only an authored fixture; no demonstrated semantic learning.",
                "support_ids": [proposal["evidence"][0]["id"]]}

    def complete(self, proposal, curation=None):
        return self.loom.complete_origami(proposal["handoff_id"], curation or self.curation(proposal),
                                          workspace=self.workspace)

    def test_actual_answer_retains_sources_correction_and_pending_review_without_a_command(self):
        # The trusted conversion constructs a fixed envelope in this process.
        with patch("subprocess.Popen", side_effect=AssertionError("No command may launch")), \
                patch("socket.socket", side_effect=AssertionError("No network may open")), \
                patch.object(origami, "ProviderCommand", side_effect=AssertionError("No command provider")):
            proposal = self.preview()
            result = self.complete(proposal)
        self.assertEqual(result["state"], "COMPLETED_PROVISIONAL")
        self.assertFalse(result["weight_update"])
        self.assertEqual(result["semantic_learning"], "NOT_ESTABLISHED")
        self.assertEqual(result["support_review"], "NOT_REVIEWED")
        self.assertIsNone(result["support_request_error"])
        packet = json.loads(Path(result["support_request"]).read_text(encoding="utf-8"))
        history = json.loads(packet["learning_context"]["context"])["history"]
        original = next(row for row in history if row["outcome_handle"] == self.original["outcome_handle"])
        self.assertEqual(original["outcome"]["status"], "FAILED")
        self.assertEqual(original["reviews"][0]["review"]["review"]["correction"], self.correction)
        self.assertEqual(packet["evidence"][0]["text"], proposal["evidence"][0]["text"])
        outcome = result["learning_outcome"]["outcome_handle"]
        self.assertEqual(self.memory.read_outcome(outcome)["status"], "COMPLETED")
        self.assertFalse(self.memory.fold_eligibility(outcome)["eligible"])
        self.assertIn("Observe the actual result first.", Path(result["artifact"]).read_text(encoding="utf-8"))
        before = snapshot(self.root)
        self.assertEqual(self.complete(proposal), result)
        self.assertEqual(before, snapshot(self.root))
        changed = self.curation(proposal)
        changed["answer"] = "Different submission"
        with self.assertRaisesRegex(ValueError, "different curation"):
            self.complete(proposal, changed)
        self.assertEqual(before, snapshot(self.root))

    def test_unresolved_is_retained_without_an_answer_or_support_pass(self):
        proposal = self.preview()
        result = self.complete(proposal, self.curation(proposal, "UNRESOLVED"))
        self.assertEqual(result["state"], "UNRESOLVED")
        self.assertIsNone(result["support_request"])
        self.assertEqual(result["support_review"], "NOT_REVIEWED")
        self.assertFalse(result["weight_update"])
        self.assertIsNotNone(result["learning_outcome"]["outcome_handle"])
        self.assertFalse(self.memory.fold_eligibility(result["learning_outcome"]["outcome_handle"])["eligible"])

    def test_source_and_advice_drift_retain_actual_failure_without_reusing_proposal(self):
        for kind in ("source", "advice", "invalid"):
            with self.subTest(kind=kind):
                proposal = self.preview()
                curation = self.curation(proposal)
                if kind == "source":
                    (self.source / "note.txt").write_text("Changed source fixture.\n", encoding="utf-8")
                elif kind == "advice":
                    self.loom.correct(self.original["outcome_handle"], author="Second fixture reviewer",
                        correction="A later correction must reach a new proposal.", event_id="later-review")
                else:
                    curation["support_ids"] = ["invented-evidence-id"]
                result = self.complete(proposal, curation)
                self.assertEqual(result["state"], "FAILED")
                self.assertEqual(self.memory.read_outcome(result["outcome_handle"])["status"], "FAILED")
                self.assertIn("changed" if kind != "invalid" else "actual source", result["error"])
                self.assertEqual(self.complete(proposal, curation), result)

    def test_review_packet_failure_preserves_the_actual_completed_conversion(self):
        proposal = self.preview()
        with patch("lodestone_machinery.support_review.support_request", side_effect=ValueError("authored review-packet failure")):
            result = self.complete(proposal)
        self.assertEqual(result["state"], "COMPLETED_PROVISIONAL")
        self.assertEqual(result["support_request_error"], "authored review-packet failure")
        self.assertIsNone(result["support_request"])
        self.assertEqual(self.memory.read_outcome(result["learning_outcome"]["outcome_handle"])["status"], "COMPLETED")

    def test_proposal_digest_and_immutable_publication_are_checked(self):
        proposal = self.preview()
        directory = self.workspace / proposal["handoff_id"]
        original = (directory / "proposal.json").read_bytes()
        with self.assertRaises(FileExistsError):
            handoff._write(directory / "proposal.json", {"foreign": True})
        self.assertEqual((directory / "proposal.json").read_bytes(), original)
        changed = json.loads(original)
        changed["question"] = "Tampered question"
        (directory / "proposal.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "proposal changed"):
            self.complete(proposal)
        self.assertFalse((directory / "curation.json").exists())

    def child(self, script, *args):
        prefix = "import sys\nsys.path.insert(0, sys.argv[1])\n"
        process = subprocess.Popen([sys.executable, "-I", "-B", "-u", "-c", prefix + script,
                                    str(RUNTIME / "src"), *map(str, args)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)

    def wait_ready(self, process, marker):
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.communicate()[1])
            time.sleep(.02)
        self.assertTrue(marker.exists(), "Child did not reach the controlled boundary")

    def test_native_origami_lock_contends_and_process_exit_releases(self):
        organ = origami.Origami(self.root / "direct origami")
        run = organ.start(self.source, ["note.txt"], "Fixture?", "fixture", "authored test")
        directory = organ._dir(run["run_id"])
        marker = self.root / "lock-ready"
        child = self.child('''
import time
from pathlib import Path
from lodestone_machinery.origami import Origami
organ = Origami(sys.argv[2])
with organ._lock(organ._dir(sys.argv[3])):
 Path(sys.argv[4]).write_text('ready')
 time.sleep(60)
''', organ.workspace, run["run_id"], marker)
        self.wait_ready(child, marker)
        with self.assertRaisesRegex(ValueError, "busy"):
            with organ._lock(directory):
                self.fail("Two processes acquired one Origami run")
        child.kill()
        child.communicate(timeout=15)
        with organ._lock(directory):
            self.assertTrue((directory / "intent.json").is_file())

    def test_killed_handoff_retains_submission_and_never_retries_conversion(self):
        proposal = self.preview()
        config = self.root / "child.json"
        config.write_text(json.dumps({"memory": str(self.memory.workspace), "seeds": self.seeds,
            "workspace": str(self.workspace), "id": proposal["handoff_id"],
            "curation": self.curation(proposal)}), encoding="utf-8")
        marker = self.root / "submission-ready"
        child = self.child('''
import json, time
from pathlib import Path
from lodestone_machinery.loom_handoff import LoomHandoff
from lodestone_machinery.loom_unified import UnifiedLoom
config = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
organ = LoomHandoff(UnifiedLoom(config['memory'], config['seeds']), config['workspace'])
def stop_after_submission(*args):
 Path(sys.argv[3]).write_text('ready')
 time.sleep(60)
 raise AssertionError('The fixture must be killed at this boundary')
organ._convert = stop_after_submission
organ.complete(config['id'], config['curation'])
''', config, marker)
        self.wait_ready(child, marker)
        submitted = self.workspace / proposal["handoff_id"] / "curation.json"
        self.assertEqual(json.loads(submitted.read_text(encoding="utf-8")), self.curation(proposal))
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError, "busy"):
                self.complete(proposal)
        child.kill()
        child.communicate(timeout=15)
        before = snapshot(self.root)
        with patch.object(LoomHandoff, "_convert", side_effect=AssertionError("Never retry")), \
                self.assertRaisesRegex(ValueError, "interrupted after submission"):
            self.complete(proposal)
        self.assertEqual(before, snapshot(self.root))
        self.assertFalse(submitted.with_name("result.json").exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows reparse/stream handling")
    def test_windows_source_stream_device_and_junction_paths_refuse_without_proposal(self):
        for source in ("note.txt:stream", "C:/outside.txt", "CON", "NUL.txt", "note.txt.", "note.txt "):
            with self.subTest(source=source), self.assertRaises((ValueError, OSError)):
                self.handoff.preview(self.original["outcome_handle"], root=self.source, paths=[source])
        alias = self.root / "source-junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(self.source)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(alias.rmdir)
        with self.assertRaises((ValueError, OSError)):
            self.handoff.preview(self.original["outcome_handle"], root=alias, paths=["note.txt"])
        self.assertFalse(self.workspace.exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows held-handle custody")
    def test_windows_reads_hold_source_identity_and_workspace_aliases_are_not_hidden(self):
        source = self.source / "note.txt"
        replacement = self.source / "replacement.txt"
        replacement.write_text("Unexpected replacement", encoding="utf-8")
        original_read = portable_files.os.read
        attempted = []
        def during_read(fd, count):
            if not attempted:
                attempted.append(True)
                with self.assertRaises(PermissionError):
                    os.replace(replacement, source)
                with self.assertRaises(PermissionError):
                    source.write_bytes(b"Unexpected overwrite")
            return original_read(fd, count)
        with patch.object(portable_files.os, "read", side_effect=during_read):
            observed = anchor(self.source, "note.txt")
        self.assertTrue(attempted)
        self.assertIn("observed result", observed["text"])
        target = self.root / "foreign handoff target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("Preserve foreign state", encoding="utf-8")
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(self.workspace), str(target)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(self.workspace.rmdir)
        before = snapshot(target)
        with self.assertRaises((ValueError, OSError)):
            self.preview()
        self.assertEqual(snapshot(target), before)

    @unittest.skipUnless(os.name == "nt", "Native Windows immutable flush failures")
    def test_windows_flush_failure_never_publishes_submission(self):
        proposal = self.preview()
        directory = self.workspace / proposal["handoff_id"]
        with patch.object(portable_files.os, "fsync", side_effect=OSError("authored flush failure")), \
                self.assertRaisesRegex(OSError, "flush failure"):
            self.complete(proposal)
        self.assertFalse((directory / "curation.json").exists())
        self.assertFalse((directory / "result.json").exists())

    @unittest.skipUnless(os.name != "nt", "Preserve historical POSIX source links")
    def test_posix_in_root_source_alias_behavior_is_unchanged(self):
        (self.source / "alias.txt").symlink_to(self.source / "note.txt")
        self.assertEqual(anchor(self.source, "alias.txt")["text"], anchor(self.source, "note.txt")["text"])


if __name__ == "__main__":
    unittest.main()

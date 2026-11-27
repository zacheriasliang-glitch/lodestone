"""Native Pando init/verify on disposable boards; no models or host records."""
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
from lodestone_pando.store import Board


class PortablePandoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Pando native ü ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "board ü"

    def cli(self, command, *, home=None, extra=()):
        return subprocess.run([sys.executable, "-B", str(RUNTIME / "lodestone.py"),
            "pando", "--home", str(home or self.home), command, *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "ascii" if sys.platform == "win32" else "utf-8"})

    def test_actual_catalogue_initialization_and_verification_keep_receipts(self):
        bridge = OperationBridge(RUNTIME, self.base / "receipts")
        for operation in ("pando.init", "pando.verify"):
            request = bridge.prepare(operation, {}, self.home)
            result = bridge.run(request, approved_digest=request["digest"])
            self.assertEqual(result["status"], "COMPLETED", result)
            payload = json.loads(result["output"])
            self.assertEqual(payload.get("status", payload.get("schema")),
                             "INTACT" if operation.endswith("verify") else "lodestone-pando/v1")
            self.assertTrue(result["cleanup"]["confirmed_stopped"])
            self.assertFalse(result["execution"]["filesystem_network_sandbox"])
            self.assertEqual(json.loads((Path(result["receipt_directory"]) / "result.json").read_bytes()), result)
        self.assertEqual(len(bridge.history()), 2)
        self.assertTrue((self.home / "pando.sqlite3").is_file())

    def test_unicode_history_and_participants_survive_reopen_and_mismatch(self):
        people = [{"id": "host", "name": "持有者", "role": "Owner"},
                  {"id": "reviewer", "name": "réviseur", "role": "Reader"}]
        config = self.base / "people.json"
        config.write_text(json.dumps(people), encoding="utf-8")
        done = self.cli("init", extra=("--participants", str(config)))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["participants"], people)
        board = Board(self.home)
        raw = "原文 — failed prediction\r\nCorrection retained, not a new weight.\n".encode()
        source = board.ingest(raw, title="原文", uri="fixture://retained", author="host")
        post = board.post(author="reviewer", shelf="commons", title="Review", body="保持原文",
                          source_ids=[source])
        before = board.state()
        config.write_text(json.dumps(people + [{"id": "other", "name": "Other", "role": "Reader"}]))
        refused = self.cli("init", extra=("--participants", str(config)))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("differs", refused.stderr)
        reopened = Board(self.home)
        self.assertEqual(reopened.state(), before)
        self.assertEqual(reopened.source(source)["text"].encode(), raw)
        self.assertEqual(reopened.state()["posts"][0]["id"], post)
        verified = self.cli("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["status"], "INTACT")

    def test_source_and_link_corruption_remain_visible_and_unrepaired(self):
        board = Board(self.home)
        source = board.ingest(b"original", title="Source", uri="fixture://source", author="host")
        with board.connection() as db:
            db.execute("UPDATE sources SET body=? WHERE id=?", (b"drift", source))
        done = self.cli("verify")
        self.assertEqual(done.returncode, 2)
        self.assertIn("identity mismatch", done.stderr)
        with board.connection() as db:
            self.assertEqual(bytes(db.execute("SELECT body FROM sources").fetchone()[0]), b"drift")
        other = Board(self.base / "linked board")
        handle = other.plumber.records.publish("pando", "fixture", {"value": "retained"})
        other._artifact("Fixture", "host", "FIXTURE", handle, [], None, "Not a learned finding")
        self.assertGreater(other.verify()["record_checks_including_repeats"], 0)
        record = other.plumber.records.path(handle)
        record.chmod(0o600)
        record.write_bytes(b"corrupt")
        refused = self.cli("verify", home=other.home)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(record.read_bytes(), b"corrupt")

    def test_two_native_sqlite_writers_commit_without_lost_history(self):
        Board(self.home)
        code = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                "from lodestone_pando.store import Board; b=Board(Path(sys.argv[2])); "
                "b.post(author='host',shelf='commons',title=sys.argv[3],body='retained')")
        runs = [subprocess.Popen([sys.executable, "-B", "-c", code, str(RUNTIME / "src"),
                                 str(self.home), title], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for title in ("first", "second")]
        self.addCleanup(lambda: [p.kill() for p in runs if p.poll() is None])
        for run in runs:
            output = run.communicate(timeout=30)
            self.assertEqual(run.returncode, 0, output)
        board = Board(self.home)
        self.assertEqual({p["title"] for p in board.state()["posts"]}, {"first", "second"})
        self.assertEqual(board.verify()["posts"], 2)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows path and scope semantics")
    def test_windows_rejects_unported_routes_and_raw_ambiguous_names(self):
        for path in (str(self.home) + ".", str(self.home) + " ", str(self.home) + ":stream"):
            done = self.cli("init", home=path)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertFalse(self.home.exists())
        done = self.cli("serve")
        self.assertEqual(done.returncode, 2)
        self.assertIn("Only Pando init and verify", done.stderr)
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(sys.platform == "win32", "Native Windows junction custody")
    def test_windows_junction_home_is_refused_without_touching_target(self):
        target, link = self.base / "target", self.base / "link"
        target.mkdir()
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True, timeout=20)
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        done = self.cli("init", home=link / "board")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

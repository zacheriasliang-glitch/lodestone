"""Native custody tests: run unchanged on Windows, Linux and macOS.

All provider results are scripted fixtures; no model, training or paid call.
"""
import hashlib
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
RUNTIME = ROOT / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_nurarihyon import portable_files as files
from lodestone_nurarihyon.common import BusyError, file_lock
from lodestone_machinery.records import RecordStore
from lodestone_machinery import loom_adapter_local as model
from desktop import platform_support as installer_files


class PortableStorage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

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
        self.assertTrue(marker.exists(), "Child failed to reach the controlled crash boundary")

    def test_native_lock_contends_times_out_and_crash_releases_without_mutating_bytes(self):
        lock, marker = self.root / "custody.lock", self.root / "ready"
        original = b"preexisting lock file is evidence, not a PID owner"
        lock.write_bytes(original)
        process = self.child('''
import time
from pathlib import Path
from lodestone_nurarihyon.common import file_lock
with file_lock(sys.argv[2]):
 Path(sys.argv[3]).write_text('ready')
 time.sleep(60)
''', lock, marker)
        self.wait_ready(process, marker)
        started = time.monotonic()
        with self.assertRaises(BusyError):
            with file_lock(lock, timeout=.15):
                self.fail("Two native processes acquired the same exclusive lock")
        self.assertGreaterEqual(time.monotonic() - started, .10)
        process.kill()
        process.communicate(timeout=15)
        with file_lock(lock, timeout=5):
            pass
        self.assertEqual(lock.read_bytes(), original)

    def test_installer_and_runtime_locks_contend_on_the_same_byte_and_release(self):
        # Both implementations cover byte 0, length 1 on Windows (LockFileEx
        # and msvcrt.locking). POSIX uses flock in both implementations.
        for backend in ("installer", "runtime"):
            with self.subTest(backend=backend):
                lock, marker = self.root / (backend + ".lock"), self.root / (backend + ".ready")
                process = self.child('''
import time
from pathlib import Path
sys.path.insert(0, sys.argv[4])
if sys.argv[5] == 'installer':
 from platform_support import file_lock
else:
 from lodestone_nurarihyon.common import file_lock
with file_lock(sys.argv[2]):
 Path(sys.argv[3]).write_text('ready')
 time.sleep(60)
''', lock, marker, ROOT / "desktop", backend)
                self.wait_ready(process, marker)
                other = file_lock if backend == "installer" else installer_files.file_lock
                expected = BusyError if backend == "installer" else BlockingIOError
                with self.assertRaises(expected):
                    with other(lock):
                        self.fail("Installer and runtime locks did not exclude each other")
                process.kill()
                process.communicate(timeout=15)
                with other(lock):
                    pass
                self.assertEqual(lock.stat().st_size, 0)

    def test_guarded_directory_allows_durable_publication(self):
        directory = files.ensure_directory(self.root / "guarded")
        with files.guarded_directory(directory):
            files.durable_write(directory / "record", b"first")
            files.durable_write(directory / "record", b"second", replace=True)
            nested = files.ensure_directory(directory / "nested")
            files.durable_write(nested / "record", b"child")
            self.assertEqual(files.read_bytes(directory / "record"), b"second")
            self.assertEqual(files.read_bytes(nested / "record"), b"child")

    def test_atomic_publish_preserves_existing_and_flush_failure_preserves_old_bytes(self):
        path = self.root / "record"
        files.durable_write(path, b"old")
        with self.assertRaises(FileExistsError):
            files.durable_write(path, b"unexpected")
        with patch.object(files.os, "fsync", side_effect=OSError("injected flush failure")):
            with self.assertRaisesRegex(OSError, "flush failure"):
                files.durable_write(path, b"new", replace=True)
        self.assertEqual(path.read_bytes(), b"old")
        files.durable_write(path, b"complete new bytes", replace=True)
        self.assertEqual(files.read_bytes(path), b"complete new bytes")
        self.assertFalse(list(self.root.glob(".pending-*")))

    def test_crash_before_publication_leaves_old_record_intact(self):
        path, marker = self.root / "record", self.root / "ready"
        files.durable_write(path, b"old record")
        process = self.child('''
import time
from pathlib import Path
from lodestone_nurarihyon import portable_files as files
def pause(*args, **kwargs):
 Path(sys.argv[3]).write_text('ready')
 time.sleep(60)
if files.WINDOWS:
 files._win_move = pause
else:
 files.os.replace = pause
files.durable_write(sys.argv[2], b'new incomplete transaction', replace=True)
''', path, marker)
        self.wait_ready(process, marker)
        process.kill()
        process.communicate(timeout=15)
        self.assertEqual(files.read_bytes(path), b"old record")
        self.assertTrue(list(self.root.glob(".pending-*")), "Interrupted temporary remains available for inspection")

    def test_record_publication_is_concurrent_idempotent_and_keeps_corruption(self):
        memory = self.root / "memory"
        store = RecordStore(memory)
        script = '''
from lodestone_machinery.records import RecordStore
print(RecordStore(sys.argv[2]).publish('bezoar', 'fixture', {'text':'literal retained bytes'}))
'''
        children = [self.child(script, memory) for _ in range(2)]
        handles = []
        for process in children:
            output, error = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, error)
            handles.append(output.strip())
        self.assertEqual(handles[0], handles[1])
        before = {str(p.relative_to(memory)): p.read_bytes() for p in memory.rglob("*") if p.is_file()}
        self.assertEqual(store.publish("bezoar", "fixture", {"text": "literal retained bytes"}), handles[0])
        self.assertEqual(before, {str(p.relative_to(memory)): p.read_bytes() for p in memory.rglob("*") if p.is_file()})
        store.path(handles[0]).write_bytes(b"damaged evidence")
        with self.assertRaises(ValueError):
            store.publish("bezoar", "fixture", {"text": "literal retained bytes"})
        self.assertEqual(store.path(handles[0]).read_bytes(), b"damaged evidence")

    def make_symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable; junction test remains mandatory")
            raise

    def test_file_symlink_refuses_reads_writes_and_locks_without_changing_target(self):
        target, link = self.root / "target", self.root / "alias"
        target.write_bytes(b"unchanged")
        self.make_symlink(link, target)
        for operation in (lambda: files.read_bytes(link), lambda: files.durable_write(link, b"bad", replace=True)):
            with self.assertRaises(ValueError):
                operation()
        with self.assertRaises(ValueError):
            with file_lock(link):
                pass
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_ancestor_symlink_refuses_record_store_and_model_custody(self):
        target, link = self.root / "target", self.root / "alias"
        target.mkdir()
        self.make_symlink(link, target, directory=True)
        for operation in (lambda: RecordStore(link / "memory"), lambda: model.VerifiedAssets(link)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Native Windows reparse-point test")
    def test_windows_junction_is_refused_without_symlink_privilege(self):
        target, link = self.root / "target", self.root / "junction"
        target.mkdir()
        (target / "record").write_bytes(b"original")
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        for operation in (lambda: files.read_bytes(link / "record"),
                          lambda: files.durable_write(link / "record", b"bad", replace=True),
                          lambda: RecordStore(link / "memory"),
                          lambda: model.VerifiedAssets(link)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual((target / "record").read_bytes(), b"original")
        self.assertFalse((target / "memory").exists())

    def test_open_file_replacement_is_refused_or_prevented_by_native_custody(self):
        path, replacement = self.root / "record", self.root / "replacement"
        path.write_bytes(b"first")
        replacement.write_bytes(b"other")
        if os.name == "nt":
            with files.open_regular(path):
                with self.assertRaises(PermissionError):
                    os.replace(replacement, path)
            self.assertEqual(path.read_bytes(), b"first")
        else:
            with self.assertRaisesRegex(ValueError, "identity changed"):
                with files.open_regular(path):
                    os.replace(replacement, path)

    def test_guarded_ancestor_cannot_redirect_nested_reads(self):
        directory = self.root / "retained"
        directory.mkdir()
        (directory / "record").write_bytes(b"original")
        if os.name == "nt":
            with files.guarded_directory(directory):
                with self.assertRaises(PermissionError):
                    directory.rename(self.root / "moved")
                self.assertEqual(files.read_bytes(directory / "record"), b"original")
        else:
            with self.assertRaisesRegex(ValueError, "Directory identity changed"):
                with files.guarded_directory(directory):
                    directory.rename(self.root / "moved")
                    directory.mkdir()
                    (directory / "record").write_bytes(b"redirected")
                    self.assertEqual(files.read_bytes(directory / "record"), b"original")
            # The failed context must not leave a stale cached directory fd.
            self.assertEqual(files.read_bytes(directory / "record"), b"redirected")

    def test_model_checks_detect_same_bytes_in_replaced_file_and_replaced_root(self):
        root = self.root / "model"
        root.mkdir()
        body = b"fixture model bytes; never loaded"
        (root / "model.safetensors").write_bytes(body)
        pins = {"model.safetensors": (len(body), hashlib.sha256(body).hexdigest())}
        with patch.object(model, "ASSETS", pins):
            assets = model.VerifiedAssets(root)
            assets.verify_unchanged(full=True)
            replacement = root / "replacement"
            replacement.write_bytes(body)
            replacement.replace(root / "model.safetensors")
            with self.assertRaisesRegex(ValueError, "identity changed"):
                assets.verify_unchanged()
            assets = model.VerifiedAssets(root)
            root.rename(self.root / "old model")
            root.mkdir()
            (root / "model.safetensors").write_bytes(body)
            with self.assertRaisesRegex(ValueError, "Model root changed"):
                assets.verify_unchanged()

    @unittest.skipUnless(os.name == "nt", "Native Windows contract")
    def test_windows_directory_fsync_refuses_explicitly(self):
        with self.assertRaises(files.UnsupportedDurability):
            files.sync_directory(self.root)

    def test_default_learning_broker_retains_scripted_failure_and_following_history(self):
        # Exercises actual RecordStore, broker locks, durable journals and memory;
        # no bootstrap/platform-service assumptions and no network/model imports.
        script = '''
import json
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery.learning_context import expand_learning_projection
memory = LearningMemory(sys.argv[2])
evidence = memory.evidence({'source':'portable storage test', 'event_id':'fixture-1', 'observed_at':'2026-11-27T00:00:00Z',
 'text':'Observed fixture only.', 'attribution':'Scripted test, not a user event.'})
seed = memory.seed({'id':'portable-test', 'correction':'Keep actual failed results.',
 'conditions':['Scripted native storage test.'], 'frame_limits':['No semantic learning claim.'],
 'evidence_handles':[evidence], 'author':'storage test'})
second_evidence = memory.evidence({'source':'portable storage test', 'event_id':'fixture-2',
 'observed_at':'2026-11-27T00:00:01Z', 'text':'Second observed fixture only.',
 'attribution':'Scripted test, not a user event.'})
memory.nacre({'id':'portable-pattern', 'pattern':'Retain failed fixture outcomes.',
 'conditions':['Scripted native storage test.'], 'evidence_handles':[evidence, second_evidence],
 'seed_handles':[seed], 'author':'storage test'})
loom = UnifiedLoom(memory, [seed])
first = loom.run(encounter='First fixture', purpose='Retain actual outcomes.', reader='scripted fixture',
 context='Storage verification.', provider=lambda packet: {'status':'FAILED',
 'response':'Partial fixture response', 'observation':{'reason':'deliberate scripted failure'}})
seen = []
def provider(packet):
 practice = json.loads(packet['context'])
 seen.extend(expand_learning_projection(practice['learning_context'])['content']['history'])
 return 'Following scripted response'
second = loom.run(encounter='Following fixture', purpose='Consume retained failure.',
 reader='scripted fixture', context='Storage verification.', provider=provider)
print(json.dumps({'first':first['status'], 'second':second['status'],
 'history':[row['status'] for row in seen], 'pending':loom.status()['pending']}))
'''
        process = self.child(script, self.root / "memory")
        output, error = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(json.loads(output), {"first": "FAILED", "second": "COMPLETED",
                                              "history": ["FAILED"], "pending": []})


if __name__ == "__main__":
    unittest.main()

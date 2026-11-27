"""Notebook locking, exact append custody and native reads; no models or network."""
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
SCRIPT = RUNTIME / "tools/local-memory/src/lodestone_memory.py"
spec = importlib.util.spec_from_file_location("portable_notebook", SCRIPT)
memory = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = memory
spec.loader.exec_module(memory)
files = memory._native_files()

CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import lodestone_memory as memory
# Exercise the Windows storage branch with real native handles on each OS.
# Windows requires no branch override and always uses its actual kernel APIs.
memory._WINDOWS = True
"""


class PortableLocalMemory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="notebook ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.notebook = self.root / "memory λ"
        self.mode = patch.object(memory, "_WINDOWS", True)
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def record(self, label="one"):
        return memory.make_record("Concurrent note " + label, "Fixture body — 窯 " + label,
            "inert attribution " + label, "note", ["fixture"], .8)

    def snapshot(self):
        return {p.relative_to(self.notebook).as_posix(): p.read_bytes()
                for p in self.notebook.rglob("*") if p.is_file() and not p.is_symlink()}

    def child(self, code, *args, encoding="utf-8"):
        process = subprocess.Popen([sys.executable, "-I", "-B", "-u", "-c", CHILD + code,
            str(SCRIPT.parent), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": encoding + ":strict"})
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)

    def completed(self, process):
        output, error = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, error.decode("utf-8", errors="replace"))
        return output

    def test_existing_bytes_schema_and_attribution_survive_append_and_reads(self):
        first = self.record()
        # Build a historical notebook using the unchanged writer. Its bytes
        # must remain an exact prefix when Windows appends CRLF records.
        with patch.object(memory, "_WINDOWS", False):
            memory.add_record(self.notebook, first)
        before = self.snapshot()
        second = self.record("two")
        memory.add_record(self.notebook, second)
        records = (self.notebook / "records.jsonl").read_bytes()
        self.assertTrue(records.startswith(before["records.jsonl"]))
        self.assertEqual((self.notebook / "state.json").read_bytes(), before["state.json"])
        self.assertEqual((self.notebook / "links.jsonl").read_bytes(), before["links.jsonl"])
        self.assertEqual(memory.load_records(self.notebook), [first, second])
        after = self.snapshot()
        self.assertEqual(memory.add_record(self.notebook, first), (first["id"], False))
        self.assertEqual(memory.verify(self.notebook), [])
        hits = memory.search(self.notebook, "fixture")
        self.assertEqual({row.record["id"] for row in hits}, {first["id"], second["id"]})
        self.assertEqual(self.snapshot(), after)
        # The source field is attribution, never a command or file to ingest.
        self.assertEqual({hit.record["source"] for hit in hits},
                         {"inert attribution one", "inert attribution two"})

    def test_concurrent_add_search_verify_keeps_all_records_and_one_duplicate(self):
        memory.ensure_root(self.notebook)
        start = self.root / "start"
        processes = [self.child('''
import json, time
root=Path(sys.argv[2]); start=Path(sys.argv[3]); worker=sys.argv[4]
deadline=time.monotonic()+20
while not start.exists():
 if time.monotonic()>deadline: raise RuntimeError('Fixture barrier timed out')
 time.sleep(.01)
for index in range(4):
 label=worker+'-'+str(index)
 memory.add_record(root,memory.make_record('Concurrent '+label,'Body '+label,'fixture '+label,'note',[],.8))
 assert memory.verify(root)==[]
 assert memory.search(root,'concurrent')
memory.add_record(root,memory.make_record('Shared','Shared body','shared fixture','note',[],.8))
print(json.dumps({'worker':worker,'records':len(memory.load_records(root))}))
''', self.notebook, start, index) for index in range(4)]
        start.write_text("go", encoding="ascii")
        for child in processes:
            self.assertIn("worker", json.loads(self.completed(child)))
        records = memory.load_records(self.notebook)
        self.assertEqual(len(records), 17)
        self.assertEqual(len({row["id"] for row in records}), 17)
        self.assertEqual(sum(row["source"] == "shared fixture" for row in records), 1)
        self.assertEqual(memory.verify(self.notebook), [])

    def test_native_lock_contention_and_process_death_release(self):
        memory.ensure_root(self.notebook)
        ready = self.root / "ready"
        child = self.child('''
import time
with memory._windows_scope(Path(sys.argv[2])):
 Path(sys.argv[3]).write_text('ready',encoding='ascii')
 time.sleep(60)
''', self.notebook, ready)
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail(child.communicate()[1])
            time.sleep(.02)
        self.assertTrue(ready.exists())
        from lodestone_nurarihyon.common import BusyError, file_lock
        with self.assertRaises(BusyError):
            with file_lock(self.notebook / memory.LOCK_NAME):
                self.fail("Second process acquired the notebook lock")
        child.kill()
        child.communicate(timeout=15)
        self.assertTrue(memory.add_record(self.notebook, self.record())[1])
        self.assertEqual(memory.verify(self.notebook), [])

    def test_failed_flush_and_malformed_prefix_preserve_existing_store(self):
        memory.add_record(self.notebook, self.record())
        before = self.snapshot()
        with patch.object(files.os, "fsync", side_effect=OSError("authored flush failure")), \
                self.assertRaisesRegex(OSError, "authored flush failure"):
            memory.add_record(self.notebook, self.record("unpublished"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.notebook.glob(".pending-*")))
        (self.notebook / "records.jsonl").write_bytes(before["records.jsonl"] + b"{unfinished")
        broken = self.snapshot()
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            memory.add_record(self.notebook, self.record("refused"))
        self.assertEqual(self.snapshot(), broken)

    def test_catalogue_bound_refuses_without_truncating_old_bytes(self):
        memory.add_record(self.notebook, self.record())
        before = self.snapshot()
        bound = len(before["records.jsonl"]) + 1
        with patch.object(memory, "MAX_CATALOGUE_BYTES", bound), \
                self.assertRaisesRegex(ValueError, "32 MiB"):
            memory.add_record(self.notebook, self.record("exceeds remaining bound"))
        self.assertEqual(self.snapshot(), before)

    def test_read_time_replacement_is_prevented_or_refused(self):
        memory.add_record(self.notebook, self.record())
        target = self.notebook / "records.jsonl"
        replacement = self.root / "replacement.jsonl"
        replacement.write_bytes(b"foreign replacement\n")
        original_read = files.os.read
        tried = []
        def read(fd, count):
            data = original_read(fd, count)
            if not tried:
                tried.append(True)
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        os.replace(replacement, target)
                else:
                    os.replace(replacement, target)
            return data
        with patch.object(files.os, "read", side_effect=read):
            if os.name == "nt":
                self.assertEqual(memory.verify(self.notebook), [])
            else:
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    memory.verify(self.notebook)
        self.assertEqual(tried, [True])

    def test_unicode_cli_output_and_source_attribution_do_not_open_sources(self):
        source = str(self.root / "never opened — 窯.txt")
        output = self.completed(self.child('''
root=Path(sys.argv[2]); source=sys.argv[3]
assert memory.main(['add',str(root),'--title','Unicode 窯','--body','Retain λ exactly','--source',source])==0
assert memory.main(['search',str(root),'retain'])==0
assert memory.main(['verify',str(root)])==0
''', self.notebook, source, encoding="ascii"))
        self.assertIn("Retain λ exactly", output.decode("utf-8"))
        self.assertIn(source, output.decode("utf-8"))
        self.assertFalse(Path(source).exists())
        self.assertIn("Retain λ exactly".encode(), (self.notebook / "records.jsonl").read_bytes())

    def test_actual_ecosystem_add_search_and_verify_commands(self):
        def command(*args):
            result = subprocess.run([sys.executable, "-B", str(RUNTIME / "lodestone.py"),
                "--workspace", str(self.root / "unused workspace"), "memory", *map(str, args)],
                cwd=self.root, capture_output=True, timeout=30,
                env={**os.environ, "PYTHONIOENCODING": "ascii:strict" if os.name == "nt" else "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            return result.stdout
        added = json.loads(command("add", self.notebook, "--title", "Authored fixture",
            "--body", "Retained Unicode λ 窯", "--source", "Unopened fixture attribution"))
        self.assertTrue(added["created"])
        found = command("search", self.notebook, "retained").decode("utf-8")
        self.assertIn("Retained Unicode λ 窯", found)
        self.assertIn(added["id"], found)
        self.assertIn(b"Memory store verified.", command("verify", self.notebook))
        self.assertFalse((self.root / "unused workspace").exists())

    def test_no_final_newline_and_unicode_line_separator_preserve_valid_json(self):
        first = self.record()
        first["title"] = "Literal Unicode separator \u2028 retained"
        memory.ensure_root(self.notebook)
        raw = json.dumps(first, ensure_ascii=False).encode()
        (self.notebook / "records.jsonl").write_bytes(raw)
        second = self.record("second")
        memory.add_record(self.notebook, second)
        self.assertTrue((self.notebook / "records.jsonl").read_bytes().startswith(raw + b"\r\n"))
        self.assertEqual(memory.load_records(self.notebook), [first, second])

    def test_alias_notebook_or_catalogue_refuses_without_rewriting_target(self):
        memory.add_record(self.notebook, self.record())
        before = self.snapshot()
        alias = self.root / "aliased notebook"
        def directory_alias(name, target):
            command = ('"' + str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
                       + '" /d /v:off /s /c "mklink /J "' + str(name) + '" "' + str(target) + '""')
            result = subprocess.run(command, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.addCleanup(os.rmdir, name)
        if os.name == "nt":
            directory_alias(alias, self.notebook)
        else:
            alias.symlink_to(self.notebook, target_is_directory=True)
        for action in (lambda: memory.add_record(alias, self.record("refused")),
                       lambda: memory.search(alias, "fixture"), lambda: memory.verify(alias)):
            with self.assertRaises((ValueError, OSError)):
                action()
        self.assertEqual(self.snapshot(), before)
        records = self.notebook / "records.jsonl"
        preserved = self.notebook / "preserved-original.jsonl"
        records.rename(preserved)
        foreign = self.root / "foreign"
        foreign.mkdir()
        marker = foreign / "records.jsonl"
        marker.write_bytes(before["records.jsonl"])
        if os.name == "nt":
            directory_alias(records, foreign)
        else:
            records.symlink_to(marker)
        for action in (lambda: memory.add_record(self.notebook, self.record("refused")),
                       lambda: memory.search(self.notebook, "fixture"), lambda: memory.verify(self.notebook)):
            with self.assertRaises((ValueError, OSError)):
                action()
        self.assertEqual(marker.read_bytes(), before["records.jsonl"])
        self.assertEqual(preserved.read_bytes(), before["records.jsonl"])


if __name__ == "__main__":
    unittest.main()

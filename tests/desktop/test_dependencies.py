"""Dependency custody fixtures: no network, package installation or inference."""
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from desktop import dependencies as d


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def artifact(self, content=b"verified public fixture", filename="fixture.bin"):
        return {"filename": filename, "url": "https://example.org/" + filename,
                "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}

    def cached(self, content, artifact):
        path = self.cache / artifact["sha256"]
        path.write_bytes(content)
        return path

    @contextmanager
    def no_process(self):
        with patch.object(d.subprocess, "run", side_effect=AssertionError("Unexpected process")) as run:
            yield run
            run.assert_not_called()

    def archive(self, members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content, target in members:
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                if target is not None:
                    info.type, info.linkname = tarfile.SYMTYPE, target
                    archive.addfile(info)
                else:
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
        raw = buffer.getvalue()
        artifact = self.artifact(raw, "fixture.tar.gz")
        artifact["executable"] = "bin/tool"
        self.cached(raw, artifact)
        return artifact

    def test_verified_offline_cache_reuse_never_downloads(self):
        raw = b"verified public fixture"
        artifact = self.artifact(raw)
        path = self.cached(raw, artifact)
        with self.no_process():
            self.assertEqual(d.fetch(artifact, self.cache, offline=True), path)

    def test_offline_missing_and_corrupt_cache_fail_without_replacement(self):
        artifact = self.artifact()
        with self.no_process(), self.assertRaises(FileNotFoundError):
            d.fetch(artifact, self.cache, offline=True)
        path = self.cached(b"wrong bytes", artifact)
        with self.no_process(), self.assertRaisesRegex(ValueError, "pinned hash"):
            d.fetch(artifact, self.cache)
        self.assertEqual(path.read_bytes(), b"wrong bytes")

    def test_interrupted_download_resumes_but_unverified_bytes_are_not_cache(self):
        raw = b"complete verified download"
        artifact = self.artifact(raw)
        partial = self.cache / (artifact["sha256"] + ".partial")
        def interrupted(command, **kwargs):
            partial.write_bytes(raw[:8])
            raise subprocess.CalledProcessError(28, command)
        with patch.object(d.subprocess, "run", side_effect=interrupted):
            with self.assertRaises(subprocess.CalledProcessError):
                d.fetch(artifact, self.cache)
        self.assertTrue(partial.is_file())
        self.assertFalse((self.cache / artifact["sha256"]).exists())
        def complete(command, **kwargs):
            self.assertIn("--continue-at", command)
            self.assertIn("=https", command)
            partial.write_bytes(raw)
        with patch.object(d.subprocess, "run", side_effect=complete):
            self.assertEqual(d.fetch(artifact, self.cache).read_bytes(), raw)
        self.assertFalse(partial.exists())

    def test_incorrect_download_cannot_be_extracted(self):
        artifact = self.artifact(b"expected archive", "fixture.tar.gz")
        artifact["executable"] = "bin/tool"
        def wrong(command, **kwargs):
            Path(command[command.index("--output") + 1]).write_bytes(b"wrong archive")
        destination = self.root / "unpacked"
        with patch.object(d.subprocess, "run", side_effect=wrong):
            with self.assertRaisesRegex(ValueError, "pinned hash"):
                d.unpack(artifact, self.cache, destination)
        self.assertFalse(destination.exists())

    def test_artifacts_reject_non_https_invalid_digest_and_path_names(self):
        for change in ({"url": "file:///etc/passwd"}, {"sha256": "not-a-digest"},
                       {"filename": "../outside"}, {"size": True}):
            with self.subTest(change=change), self.no_process(), self.assertRaises(ValueError):
                d.fetch({**self.artifact(), **change}, self.cache)

    def test_cache_symlink_cannot_redirect_reads_or_partial_writes(self):
        artifact = self.artifact()
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        cached = self.cache / artifact["sha256"]
        cached.symlink_to(outside)
        with self.no_process(), self.assertRaises(ValueError):
            d.fetch(artifact, self.cache)
        cached.unlink()
        (self.cache / (artifact["sha256"] + ".partial")).symlink_to(outside)
        with self.no_process(), self.assertRaises(ValueError):
            d.fetch(artifact, self.cache)
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_verified_archive_reuses_exact_tree_and_detects_modified_executable(self):
        artifact = self.archive([("bin/tool", b"#!/bin/sh\nexit 0\n", None),
                                 ("bin/alias", b"", "tool")])
        destination = self.root / "unpacked"
        with self.no_process():
            executable = d.unpack(artifact, self.cache, destination, offline=True)
            self.assertEqual(d.unpack(artifact, self.cache, destination, offline=True), executable)
        executable.write_bytes(b"changed executable")
        with self.no_process(), self.assertRaisesRegex(ValueError, "differs"):
            d.unpack(artifact, self.cache, destination, offline=True)

    def test_archive_traversal_and_escape_symlinks_are_rejected_before_extraction(self):
        for members in ([('../outside', b'injected', None)],
                        [('bin/tool', b'', '../../outside')]):
            with self.subTest(members=members):
                artifact = self.archive(members)
                destination = self.root / "unpacked"
                with self.no_process(), self.assertRaises(ValueError):
                    d.unpack(artifact, self.cache, destination, offline=True)
                self.assertFalse(destination.exists())
                self.assertFalse((self.root / "outside").exists())

    def test_existing_unexpected_file_fails_archive_verification(self):
        artifact = self.archive([("bin/tool", b"#!/bin/sh\n", None)])
        destination = self.root / "unpacked"
        d.unpack(artifact, self.cache, destination, offline=True)
        (destination / "sitecustomize.py").write_text("unexpected startup code")
        with self.no_process(), self.assertRaisesRegex(ValueError, "differs"):
            d.unpack(artifact, self.cache, destination, offline=True)

    def test_doctor_is_read_only_and_detects_model_blob_tampering(self):
        payload = self.root / "payload"
        worker = payload / "runtime/src/lodestone_machinery/loom_runtime_worker.py"
        worker.parent.mkdir(parents=True)
        worker.write_text("# fixture only")
        token = self.artifact(b"tokenizer", "tokenizer.json")
        blob = self.artifact(b"GGUF-fixture", "model.gguf")
        manifest = self.artifact(b"manifest", "manifest.json")
        archive = self.archive([("bin/tool", b"#!/bin/sh\nexit 0\n", None)])
        lock = {"schema": d.SCHEMA, "artifacts": {"ollama": archive}, "environments": {},
                "tokenizer": {"files": [token]}, "model": {"digest": manifest["sha256"],
                "blobs": [blob], "manifest": manifest, "manifest_path": "manifests/model"}}
        lock_path = payload / "dependencies.lock.json"
        lock_path.write_text(json.dumps(lock))
        deps = self.root / "deps/sets" / hashlib.sha256(lock_path.read_bytes()).hexdigest()
        d.unpack(archive, self.cache, deps / "ollama", offline=True)
        for artifact, path, raw in [(token, "tokenizer/tokenizer.json", b"tokenizer"),
                                    (blob, "ollama-models/blobs/sha256-" + blob["sha256"], b"GGUF-fixture"),
                                    (manifest, "ollama-models/manifests/model", b"manifest")]:
            target = deps / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        d._json(deps / "dependencies.json", {"evidence": {
            "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(), "cache": str(self.cache)}})
        before = d._inventory(self.root)
        with self.no_process():
            self.assertEqual(d.verify(self.root, payload)["status"], "VERIFIED")
        self.assertEqual(d._inventory(self.root), before)
        (deps / "ollama-models/blobs" / ("sha256-" + blob["sha256"])).write_bytes(b"tampered")
        with self.no_process(), self.assertRaisesRegex(ValueError, "model blob"):
            d.verify(self.root, payload)

    def test_two_payload_locks_keep_distinct_dependency_sets_and_prior_verification(self):
        archive = self.archive([("bin/tool", b"#!/bin/sh\nexit 0\n", None)])
        token = self.artifact(b"tokenizer", "tokenizer.json")
        self.cached(b"tokenizer", token)
        payloads = []
        for epoch in ("A", "B"):
            payload = self.root / ("payload-" + epoch)
            worker = payload / "runtime/src/lodestone_machinery/loom_runtime_worker.py"
            worker.parent.mkdir(parents=True)
            worker.write_text("# fixture worker")
            raw = ("manifest-" + epoch).encode()
            manifest = self.artifact(raw, "manifest.json")
            self.cached(raw, manifest)
            lock = {"schema": d.SCHEMA,
                "platform": {"system": "Darwin", "machine": "x86_64", "minimum_macos": "12.0"},
                "artifacts": {name: {**archive, "version": version} for name, version in (
                    ("uv", "1"), ("python_worker", "3.12.12"), ("python_gui", "3.14.0"), ("ollama", "1"))},
                "environments": {name: {"python": "python_" + name, "wheels": [], "epoch": epoch}
                                 for name in ("gui", "worker")},
                "tokenizer": {"files": [token]},
                "model": {"digest": manifest["sha256"], "manifest": manifest,
                          "manifest_path": "manifests/model", "blobs": []}}
            (payload / "dependencies.lock.json").write_text(json.dumps(lock))
            payloads.append(payload)

        def environment(name, spec, python, uv, root, cache, *, offline):
            env = root / name
            binary = d.venv_python(env)
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture executable " + spec["epoch"])
            d._json(root / (name + ".installed.json"), {
                "spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                "files": d._inventory(env)})
            return binary

        with patch.object(d.platform, "system", return_value="Darwin"), \
             patch.object(d.platform, "machine", return_value="x86_64"), \
             patch.object(d.platform, "mac_ver", return_value=("12.7", (), "")), \
             patch.object(d, "_install_environment", side_effect=environment), \
             patch.object(d, "_run", return_value="fixture tokenizer"), self.no_process():
            first = d.ensure(self.root, payloads[0], self.cache, offline=True)
            first_set = Path(first["evidence"]["dependency_set"])
            retained = d._inventory(first_set)
            second = d.ensure(self.root, payloads[1], self.cache, offline=True)
            self.assertNotEqual(first["worker_python"], second["worker_python"])
            self.assertNotEqual(first["assets_dir"], second["assets_dir"])
            self.assertEqual(d._inventory(first_set), retained)
            self.assertEqual(d.verify(self.root, payloads[0])["model_digest"], first["model_digest"])
            self.assertEqual(d.verify(self.root, payloads[1])["model_digest"], second["model_digest"])
            path_fields = ("gui_python", "worker_python", "assets_dir", "ollama_binary",
                           "ollama_models", "model_digest")
            expected = {name: first[name] for name in path_fields}
            self.assertEqual(d.verify(self.root, payloads[0])["paths"], expected)
            # A stale/tampered installation receipt cannot make doctor endorse
            # the other release's executable, asset or model paths.
            receipt_path = first_set / "dependencies.json"
            receipt = json.loads(receipt_path.read_text())
            receipt.update({name: second[name] for name in path_fields})
            d._json(receipt_path, receipt)
            before = d._inventory(first_set)
            self.assertEqual(d.verify(self.root, payloads[0])["paths"], expected)
            self.assertEqual(d._inventory(first_set), before)

    def test_public_lock_has_binary_wheels_and_no_worker_torch(self):
        lock = json.loads(d.LOCK_PATH.read_text())
        self.assertEqual(lock["platform"]["machine"], "x86_64")
        self.assertEqual(lock["model"]["digest"], lock["model"]["manifest"]["sha256"])
        for spec in lock["environments"].values():
            for artifact in spec["wheels"]:
                d._artifact(artifact)
                self.assertTrue(artifact["filename"].endswith(".whl"))
        worker = {a["name"]: a["version"] for a in lock["environments"]["worker"]["wheels"]}
        self.assertNotIn("torch", worker)
        self.assertNotIn("accelerate", worker)
        self.assertEqual(worker["jinja2"], "3.1.6")
        self.assertEqual(worker["transformers"], "4.51.3")


if __name__ == "__main__":
    unittest.main()

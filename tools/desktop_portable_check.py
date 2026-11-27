#!/usr/bin/env python3
"""Native public-runner installation checks; local inference requires explicit opt-in.

All installed state and evidence belong to a new RUNNER_TEMP directory. Evidence
uses the existing bounded tar.gz/base64 log envelope, with no artifact/cache
storage action. Scripted lifecycle checks are not semantic learning evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import traceback
import zipfile

import desktop_mechanical_check as mechanical
import desktop_prior_evidence as evidence_format

TARGETS = {"macos-x86_64": "darwin", "ubuntu-24.04-x86_64": "linux", "windows-x86_64": "win32"}
SOURCE_TESTS = ("test_portable_process.py", "test_portable_storage.py", "test_platform_install.py",
                "test_portable_ci.py", "test_tokenizer_dependencies.py", "test_decision_format.py",
                "test_dependencies.py", "test_build.py", "test_dependency_custody.py", "test_dependency_archives.py", "test_windows_bootstrap.py",
                "test_mechanical_preview.py")
# Real local record publication, retained correction delivery, practice route
# options and full-history preservation with scripted providers; no model calls.
SOURCE_TESTS += ("test_linux_command_fence.py", "test_smoke_evidence.py", "test_learning_archive_custody.py", "test_decision_retention.py",
                 "test_practice_selection.py", "test_practice_voxels.py", "test_reader_history_window.py")
READERS = {"LODESTONE_OLD_GUI_SOURCE": "b82b78ddba3b8067a7c09df6c4c0a18084a7e59f",
           "LODESTONE_PREVIOUS_GUI_SOURCE": "655fa55672a4c3ae2d0284da881921b7bed97c78"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def select_mode(event, requested, expected, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("An exact source commit is required")
    requested = mechanical.flag(requested)
    if event == "pull_request":
        if requested:
            raise ValueError("Pull requests cannot request local inference")
        return "mechanical_only"
    if event != "workflow_dispatch" or expected != commit:
        raise ValueError("Manual checks require the exact expected checkout commit")
    return "local_inference_smoke" if requested else "mechanical_only"


def extract_archive(archive, destination):
    """Extract only bounded regular archive entries into a new non-Git tree."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Archive extraction destination must be new")
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if not entries or len(entries) > 10000 or sum(item.file_size for item in entries) > 512 * 1024 * 1024:
            raise ValueError("Source archive exceeds its extraction bounds")
        seen, roots = set(), set()
        for item in entries:
            if item.orig_filename != item.filename:
                raise ValueError("Source archive member requires filename normalization")
            name = item.orig_filename.rstrip("/")
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or str(path) != name or ".." in path.parts
                    or ".git" in path.parts or "\\" in name or ":" in name
                    or name.casefold() in seen or item.file_size > 64 * 1024 * 1024):
                raise ValueError("Unsafe or duplicate source archive path")
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Source archive contains a link or special file")
            seen.add(name.casefold())
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError("Expected one source archive root")
        destination.mkdir(parents=True)
        for item in entries:
            path = destination.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(item) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if os.name != "nt":
                path.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
    return destination / roots.pop()


def windows_batch_command(script, arguments):
    """Quote a fixed CI batch invocation for cmd's grammar, with shell=False."""
    executable = os.environ.get("COMSPEC", "cmd.exe")
    values = [str(script), *map(str, arguments)]
    if any(character in value for value in (executable, *values) for character in '&|<>^()%!"\r\n\x00'):
        raise ValueError("Unexpected command character in CI batch arguments")
    return '"' + executable + '" /d /v:off /s /c "' + " ".join('"' + value + '"' for value in values) + '"'


def bootstrap_command(payload, target, root, app, cache):
    if target == "windows-x86_64":
        return windows_batch_command(payload / "Install Lodestone.cmd", ["--root", root,
            "--app", app, "--cache", cache, "--no-open"])
    name = "Install Lodestone.command" if target == "macos-x86_64" else "Install Lodestone.sh"
    return ["/bin/bash", str(payload / name), "--root", str(root), "--app", str(app),
            "--cache", str(cache), "--no-open"]


def wrapper_command(target, root, app, active):
    if target == "macos-x86_64":
        return [str(app / "Contents/MacOS/Lodestone"), "--verify-startup"], "installed_app_wrapper"
    if target == "windows-x86_64":
        return windows_batch_command(app, ["--verify-startup"]), "installed_cmd_wrapper"
    entries = [line[5:] for line in app.read_text(encoding="utf-8").splitlines() if line.startswith("Exec=")]
    if len(entries) != 1:
        raise ValueError("Installed desktop file has no unique Exec command")
    # Decode desktop string escaping before the Exec argument grammar.
    command = [value.replace("\\$", "$").replace("\\`", "`")
               for value in shlex.split(entries[0].replace("\\\\", "\\").replace("%%", "%"))]
    if command != [active["worker_python"], "-B", str(root / "launch.py")]:
        raise ValueError("Installed desktop command differs from the active launcher")
    return command + ["--verify-startup"], "desktop_exec_command"


class Check:
    def __init__(self, source, work, target):
        self.source, self.work, self.target = Path(source).resolve(), Path(work).resolve(), target
        self.evidence = self.work / "evidence"
        self.root = self.work / "fresh preview"
        self.cache = self.work / "download cache"
        suffix = ".app" if target == "macos-x86_64" else ".cmd" if target == "windows-x86_64" else ".desktop"
        self.app = self.work / ("Lodestone Preview" + suffix)

    def run(self, label, command, *, timeout=900, env=None):
        started = time.monotonic()
        print("START " + label, flush=True)
        log = self.evidence / "logs" / (label + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        outcome = {"command": command if isinstance(command, str) else [str(value) for value in command], "status": "RUNNING"}
        try:
            with log.open("wb") as output:
                result = subprocess.run(command, cwd=self.source, env=env, stdout=output,
                                        stderr=subprocess.STDOUT, timeout=timeout, check=False)
            outcome["returncode"] = result.returncode
            if result.returncode:
                # Keep the complete bounded log for the final envelope and give
                # the live job a short diagnostic tail on any failed subprocess.
                print(log.read_bytes()[-16000:].decode("utf-8", errors="replace"), flush=True)
                raise subprocess.CalledProcessError(result.returncode, command)
            outcome["status"] = "PASSED"
        except BaseException as exc:
            outcome.update(status="FAILED", error_type=type(exc).__name__)
            raise
        finally:
            outcome["elapsed_seconds"] = round(time.monotonic() - started, 3)
            mechanical.write(self.evidence / (label + ".json"), outcome)
        print("PASS " + label, flush=True)

    def source_checks(self):
        failures, completed = [], []
        commands = [("source-provenance", [sys.executable, "-B", "desktop/verify_payload.py"])]
        for name in SOURCE_TESTS:
            if not (self.source / "tests/desktop" / name).is_file():
                failures.append({"check": name, "error": "Required native test suite is missing"})
                continue
            commands.append(("source-" + name.removesuffix(".py"), [sys.executable, "-B", "-m", "unittest",
                "discover", "-s", "tests/desktop", "-p", name, "-v"]))
        for label, command in commands:
            try:
                self.run(label, command, timeout=300)
                completed.append(label)
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append({"check": label, "error": type(exc).__name__})
        mechanical.write(self.evidence / "source-checks.json", {
            "status": "FAILED" if failures else "PASSED", "passed": completed, "failed": failures})
        if failures:
            raise ValueError("Native source checks failed: " + ", ".join(row["check"] for row in failures))

    def build(self):
        output = self.work / "download"
        self.run("build", [sys.executable, "-B", "desktop/build.py", "--target", self.target, "--output", str(output)])
        built = read(output / "BUILD.json")
        archives = list(output.glob("*.zip"))
        if len(archives) != 1 or built["archive"] != archives[0].name or digest(archives[0]) != built["sha256"]:
            raise ValueError("Built archive does not match its retained identity")
        if (output / "SHA256SUMS.txt").read_text().strip() != built["sha256"] + "  " + built["archive"]:
            raise ValueError("Download checksum sidecar differs")
        self.payload = extract_archive(archives[0], self.work / "extracted")
        mechanical.payload_files(self.payload)
        mechanical.write(self.evidence / "download-identity.json", built)

    def install(self, label):
        self.run(label, bootstrap_command(self.payload, self.target, self.root, self.app, self.cache), timeout=1800)
        self.active = read(self.root / "ACTIVE.json")

    def previous_readers(self):
        env = dict(os.environ)
        for name, commit in READERS.items():
            self.run("fetch-" + commit[:12], ["git", "fetch", "--no-tags", "--depth=1", "origin", commit])
            archive = self.work / (commit + ".zip")
            with archive.open("xb") as output:
                subprocess.run(["git", "archive", "--format=zip", commit, "desktop/gui"], cwd=self.source,
                               stdout=output, stderr=subprocess.PIPE, timeout=60, check=True)
            old = extract_archive(archive, self.work / ("reader-" + commit))
            env[name] = str(old / "gui")
        return env

    def gui(self):
        self.run("gui-save-reopen", [self.active["gui_python"], "-B", "tools/desktop_mechanical_check.py", "gui",
            "--tests", "tests/desktop_gui", "--payload", self.active["release"], "--evidence", str(self.evidence)],
            timeout=600, env=self.previous_readers())
        for number in (1, 2):
            receipt = self.root / "startup-verification.json"
            if receipt.exists():
                receipt.rename(self.evidence / ("startup-before-" + str(number) + ".json"))
            if number == 1:
                command = [self.active["gui_python"], "-B", str(Path(self.active["release"]) / "run_loom.py"),
                           "--root", str(self.root), "--verify-startup"]
                method = "direct_gui_python"
            else:
                command, method = wrapper_command(self.target, self.root, self.app, self.active)
            try:
                self.run("gui-startup-" + str(number), command, timeout=120)
            except subprocess.CalledProcessError as error:
                if self.target == "ubuntu-24.04-x86_64" and number == 1 and error.returncode == -11:
                    # This is an authored --verify-startup fixture with no model
                    # calls. A single diagnostic replay never changes its result.
                    try:
                        for name in ("startup-verification.json", "startup-error.txt"):
                            if (self.root / name).is_file():
                                shutil.copyfile(self.root / name, self.evidence / ("gui-startup-original-failure-" + name))
                        self.run("gui-startup-native-diagnostic", ["gdb", "--batch",
                            "-ex", "set pagination off", "-ex", "run", "-ex", "thread apply all bt",
                            "-ex", "info sharedlibrary", "-ex", "info proc mappings", "--args", *command], timeout=120)
                    except (OSError, subprocess.SubprocessError):
                        pass  # Preserve the original failed GUI invocation.
                raise
            result = read(receipt)
            if result.get("shown") is not True or not result.get("models") or result.get("model_calls") != 0:
                raise ValueError("Normal installed GUI did not prove startup without inference")
            mechanical.write(self.evidence / ("gui-startup-receipt-" + str(number) + ".json"),
                             {"launch_method": method, "receipt": result})

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): digest(path)
                for folder in ("learning", "Loom/data", "Loom/runs")
                for path in (self.root / folder).rglob("*") if path.is_file()}

    def lifecycle(self):
        original, retained = self.active, self.snapshot()
        if not retained:
            raise ValueError("No learning or saved GUI state was available for lifecycle retention")
        mechanical.write(self.evidence / "retained-before-lifecycle.json", retained)
        def unchanged():
            if any(digest(self.root / name) != identity for name, identity in retained.items()):
                raise ValueError("Installation lifecycle changed previously retained user state")
        self.install("idempotent-install")
        unchanged()
        if self.active["release_id"] != original["release_id"]:
            raise ValueError("Identical reinstall changed active release identity")
        fixture = self.work / "synthetic upgrade"
        shutil.copytree(self.payload, fixture)
        note = fixture / "README.md"
        note.write_text(note.read_text(encoding="utf-8") + "\nSynthetic CI lifecycle fixture; not a published release.\n", encoding="utf-8")
        manifest = read(fixture / "MANIFEST.json")
        manifest["version"] += "+ci-lifecycle-fixture"
        manifest["files"]["README.md"] = digest(note)
        mechanical.write(fixture / "MANIFEST.json", manifest)
        installer = [original["worker_python"], "-B", str(Path(original["release"]) / "install.py"),
                     "--root", str(self.root)]
        self.run("synthetic-upgrade", installer + ["install", "--payload", str(fixture), "--app", str(self.app), "--cache", str(self.cache)])
        upgraded = read(self.root / "ACTIVE.json")
        if upgraded["release_id"] == original["release_id"]:
            raise ValueError("Synthetic upgrade did not switch release identity")
        unchanged()
        self.run("rollback", installer + ["rollback"])
        restored = read(self.root / "ACTIVE.json")
        if restored["release_id"] != original["release_id"]:
            raise ValueError("Rollback did not restore the original release")
        unchanged()
        self.run("restored-doctor", installer + ["doctor"])
        self.run("uninstall", installer + ["uninstall"])
        wrapper = self.app / "Contents/MacOS/Lodestone" if self.target == "macos-x86_64" else self.app
        if (self.root / "ACTIVE.json").exists() or wrapper.exists():
            raise ValueError("Uninstall left an active release or application wrapper")
        unchanged()
        mechanical.write(self.evidence / "lifecycle.json", {"status": "PASSED", "retained_files_verified": len(retained),
            "original_release": original["release_id"], "synthetic_upgrade_release": upgraded["release_id"],
            "restored_release": restored["release_id"], "uninstalled": True,
            "scope": "Synthetic upgrade fixture exercises switching only; no published version or semantic learning claim."})


def preserve(check):
    """Copy only this runner fixture's outcomes; exclude installations and models."""
    destination = check.evidence / "native"
    destination.mkdir(parents=True, exist_ok=True)
    for folder in ("learning", "Loom", "transactions"):
        source = check.root / folder
        if source.is_dir():
            shutil.copytree(source, destination / folder, dirs_exist_ok=True)
    for name in ("ACTIVE.json", "UNINSTALLED.json", "startup-error.txt", "startup-verification.json", "launcher.log"):
        if (check.root / name).is_file():
            shutil.copyfile(check.root / name, destination / name)


def emit(work):
    root = Path(work) / "evidence"
    files, total = {}, 0
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Only regular CI evidence is exportable")
        relative = path.relative_to(root).as_posix()  # Canonical on Windows too.
        size = path.stat().st_size
        total += size
        if size > 8 * 1024 * 1024 or total > evidence_format.MAX_EVIDENCE or len(files) >= 4096:
            raise ValueError("CI evidence exceeds its recoverable log bound")
        files[relative] = path.read_bytes()
    if not files:
        raise ValueError("No native CI evidence was generated")
    header, chunks = evidence_format.envelope(files)
    print("LODESTONE_ARCHIVE_BEGIN " + json.dumps(header, sort_keys=True), flush=True)
    for number, chunk in enumerate(chunks):
        print("LODESTONE_ARCHIVE_CHUNK " + str(number) + " " + chunk, flush=True)
    print("LODESTONE_ARCHIVE_END " + header["archive_sha256"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    for name in ("source", "work", "source-commit", "event"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--target", choices=TARGETS, required=True)
    run.add_argument("--expected-head", default="")
    run.add_argument("--local-model-smoke", default="false")
    output = commands.add_parser("emit")
    output.add_argument("--work", required=True)
    args = parser.parse_args()
    if args.command == "emit":
        emit(args.work)
        return
    mode = select_mode(args.event, args.local_model_smoke, args.expected_head, args.source_commit)
    if sys.platform != TARGETS[args.target]:
        raise ValueError("The selected target must match this native runner")
    if os.environ.get("GITHUB_ACTIONS") != "true" or read(os.environ["GITHUB_EVENT_PATH"])["repository"]["private"] is not False:
        raise ValueError("Installation checks require a standard public GitHub Actions runner")
    check = Check(args.source, args.work, args.target)
    if not check.work.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()) or check.work.exists():
        raise ValueError("Use a new isolated directory inside RUNNER_TEMP")
    check.evidence.mkdir(parents=True)
    report = {"schema": "lodestone-portable-ci/v1", "status": "RUNNING", "target": args.target,
              "source_commit": args.source_commit, "mode": mode, "model_smoke_invoked": False,
              "paid_api_calls": 0, "weight_updates": 0, "semantic_learning": "NOT_ESTABLISHED"}
    try:
        mechanical.checkout_commit(check.source, args.source_commit)
        check.source_checks()
        check.build()
        check.install("fresh-install")
        mechanical.write(check.evidence / "installed-active.json", check.active)
        mechanical.write(check.evidence / "current-installed-identity.json", mechanical.installed_identity(
            check.source, check.payload, check.root, args.source_commit, target=check.target))
        check.gui()
        if mode == "local_inference_smoke":
            report["model_smoke_invoked"] = True
            check.run("local-smoke", [check.active["worker_python"], "-B", str(Path(check.active["release"]) / "smoke.py"),
                "--root", str(check.root), "--output", str(check.evidence / "local-smoke.json"), "--max-tokens", "512"], timeout=2700)
        check.lifecycle()
        report["status"] = "PASSED"
    except BaseException as exc:
        report.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:1000])
        (check.evidence / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        mechanical.write(check.evidence / "portable-ci.json", report)
        preserve(check)
        print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

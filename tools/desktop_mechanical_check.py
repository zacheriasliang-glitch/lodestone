#!/usr/bin/env python3
"""Bind mechanical preview checks to a commit and installed payload; no inference."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import unittest

TARGET_LOCKS = {
    "macos-x86_64": ("macOS", "dependencies.lock.json"),
    "ubuntu-24.04-x86_64": ("Linux", "dependencies.ubuntu-24.04-x86_64.lock.json"),
    "windows-x86_64": ("Windows", "dependencies.windows-x86_64.lock.json"),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def flag(value):
    if value not in ("", "false", "true"):
        raise ValueError("Unrecognized workflow boolean")
    return value == "true"


def select_mode(event, mechanical, prior, expected, github_sha, checkout_sha):
    if mechanical and prior:
        raise ValueError("Mechanical-only and prior-evidence modes are mutually exclusive")
    if event == "pull_request" and not mechanical and not prior:
        if not re.fullmatch(r"[0-9a-f]{40}", github_sha) or github_sha != checkout_sha:
            raise ValueError("Pull-request GitHub and checkout commit identities must match")
        return "mechanical_only"
    if mechanical or prior:
        if event != "workflow_dispatch":
            raise ValueError("Explicit verification modes require workflow_dispatch")
        if not re.fullmatch(r"[0-9a-f]{40}", expected) or expected != github_sha or expected != checkout_sha:
            raise ValueError("Exact expected, GitHub and checkout commit identities must match")
    return "mechanical_only" if mechanical else "prior_local_evidence" if prior else "inference_smoke"


def payload_files(root):
    root = Path(root).resolve()
    manifest = root / "MANIFEST.json"
    if manifest.is_symlink():
        raise ValueError("Manifest must be regular")
    raw = manifest.read_bytes()
    document = json.loads(raw)
    if document.get("schema") != "lodestone-desktop-payload/v1" or not document.get("files"):
        raise ValueError("Missing payload manifest")
    for name, expected in document["files"].items():
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or str(p) != name or "\\" in name or name == "MANIFEST.json":
            raise ValueError("Invalid payload path")
        if any((root / Path(*p.parts[:n])).is_symlink() for n in range(1, len(p.parts) + 1)):
            raise ValueError("Payload contains a symbolic link")
        if sha((root / name).read_bytes()) != expected:
            raise ValueError("Payload bytes changed: " + name)
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*")
              if p.is_file() and p != manifest and "__pycache__" not in p.parts}
    if actual != set(document["files"]):
        raise ValueError("Unlisted or missing payload file")
    return raw, document


def installed_identity(source, payload, preview, commit, *, target=None):
    source, payload, preview = (Path(p).resolve() for p in (source, payload, preview))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Missing exact source commit")
    active = json.loads((preview / "ACTIVE.json").read_text())
    identity = active.get("release_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("Invalid installed release identity")
    release = preview / "releases" / identity
    if active.get("release") != str(release) or release.resolve() != release:
        raise ValueError("Installed release escaped preview root")
    raw, document = payload_files(payload)
    installed_raw, installed = payload_files(release)
    if installed_raw != raw:
        raise ValueError("Installed manifest differs from extracted download")
    canonical = json.dumps(installed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if sha(canonical) != identity:
        raise ValueError("Active release identity differs from manifest")
    declared_target = document.get("target")
    if target is not None and target != declared_target:
        raise ValueError("Installed payload target differs from the selected native target")
    selected_lock = "dependencies.lock.json"
    if declared_target is not None:
        if type(declared_target) is not str or declared_target not in TARGET_LOCKS:
            raise ValueError("Unknown installed payload target")
        system, selected_lock = TARGET_LOCKS[declared_target]
        if document.get("platform") != system or document.get("architecture") != "x86_64":
            raise ValueError("Installed payload platform differs from its declared target")
        # The builder copies the selected source lock to its stable installer
        # name. Both archive names must bind those exact source bytes; no other
        # source-file substitution is permitted.
        if (selected_lock not in document["files"] or "dependencies.lock.json" not in document["files"]
                or document["files"][selected_lock] != document["files"]["dependencies.lock.json"]):
            raise ValueError("Installed default lock differs from the declared target lock")
    for name, expected in document["files"].items():
        path = source / "desktop" / (selected_lock if name == "dependencies.lock.json" else name)
        if path.is_symlink() or sha(path.read_bytes()) != expected:
            raise ValueError("Source differs from installed payload: " + name)
    return {"schema": "lodestone-mechanical-installed-identity/v1",
            "status": "SOURCE_DOWNLOAD_AND_INSTALLED_BYTES_MATCH", "source_commit": commit,
            "version": document["version"], "release_id": identity, "manifest_sha256": sha(raw),
            "files_verified": len(document["files"]), "model_calls_in_this_check": 0,
            "target": declared_target,
            "scope": "Current source and artifact identity only; no prior study attribution or semantic validation."}


def checkout_commit(source, expected):
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                            check=True, text=True, capture_output=True).stdout.strip()
    if actual != expected:
        raise ValueError("Source checkout changed from selected commit")
    changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source,
                             check=True, text=True, capture_output=True).stdout
    if changed.strip():
        raise ValueError("Source checkout has uncommitted or untracked changes")
    return actual


def gui_checks(tests, payload, evidence):
    payload, evidence = Path(payload).resolve(), Path(evidence).resolve()
    os.environ["LODESTONE_DESKTOP_TEST_PAYLOAD"] = str(payload)
    os.environ["LODESTONE_GUI_EVENT_TESTS"] = "1"
    os.environ["LODESTONE_GUI_EVIDENCE_DIR"] = str(evidence / "native/gui-fixtures")
    suite = unittest.defaultTestLoader.discover(str(tests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    success = result.wasSuccessful() and not result.skipped and result.testsRun > 0
    report = {"schema": "lodestone-installed-gui-check/v1", "status": "PASSED" if success else "FAILED",
              "payload": str(payload), "tests": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": [[test.id(), reason] for test, reason in result.skipped],
              "scope": "Actual wx events and isolated scripted native fixtures using installed payload imports; no model inference. Project serialization is exercised directly, not through a native file picker."}
    write(evidence / "installed-gui-check.json", report)
    if not success:
        raise ValueError("Installed GUI checks failed or skipped required coverage")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    mode = commands.add_parser("mode")
    for name in ("event", "mechanical-only", "verify-prior-local-evidence", "expected-head", "github-sha", "checkout-sha", "github-output", "output"):
        mode.add_argument("--" + name, required=True)
    identity = commands.add_parser("identity")
    for name in ("source", "payload", "preview", "source-commit", "output"):
        identity.add_argument("--" + name, required=True)
    identity.add_argument("--target", choices=sorted(TARGET_LOCKS))
    gui = commands.add_parser("gui")
    for name in ("tests", "payload", "evidence"):
        gui.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.command == "mode":
        selected = select_mode(args.event, flag(args.mechanical_only), flag(args.verify_prior_local_evidence),
                               args.expected_head, args.github_sha, args.checkout_sha)
        result = {"schema": "lodestone-preview-verification-mode/v1", "mode": selected,
                  "event": args.event, "github_sha": args.github_sha, "checkout_sha": args.checkout_sha,
                  "scope": "Mode selection only; no validation, inference or historical evidence replay has occurred."}
        write(args.output, result)
        with Path(args.github_output).open("a") as output:
            output.write("mode=" + selected + "\n")
    elif args.command == "identity":
        actual = checkout_commit(args.source, args.source_commit)
        result = installed_identity(args.source, args.payload, args.preview, actual, target=args.target)
        write(args.output, result)
    else:
        result = gui_checks(args.tests, args.payload, args.evidence)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

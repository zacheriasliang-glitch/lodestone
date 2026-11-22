#!/usr/bin/env python3
"""Exercise public entry points with temporary user state, without a model."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def memory_workflow(command, base):
    """Exercise the actual public CLI, including its fresh-process handoff."""
    notebook = base / "notebook"
    body = "Use a static website. The launch date is still undecided."
    source = "Project planning note"
    args = ("memory", "add", notebook, "--title", "Website launch decision",
            "--body", body, "--source", source, "--tags", "website,decision")
    added = json.loads(command("memory add", *args))
    assert added["created"] is True, added
    repeated = json.loads(command("memory identical add reuses record", *args))
    assert repeated["created"] is False and repeated["id"] == added["id"], repeated
    found = command("memory fresh-process search", "memory", "search", notebook, "website")
    assert body in found and source in found and added["id"] in found, found
    packet = base / "website-handoff.md"
    command("memory handoff", "memory", "packet", notebook, "website",
            "--max-words", "300", "--output", packet)
    text = packet.read_text(encoding="utf-8")
    assert body in text and source in text and len(text.split()) <= 300, text
    command("memory verify", "memory", "verify", notebook)
    missing = command("memory unrelated query", "memory", "search", notebook,
                      "zxqvunmatchedtoken")
    assert "No matching records." in missing, missing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-only", action="store_true",
                        help="Run only the notebook CLI workflow")
    options = parser.parse_args()
    checks = []
    with tempfile.TemporaryDirectory(prefix="lodestone-new-user-") as temp:
        base = Path(temp).resolve()
        home = base / "home"
        home.mkdir()
        workspace = base / "workspace"
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "PYTHONDONTWRITEBYTECODE": "1"}

        def command(label, *args, expected_exit=0):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "lodestone.py"),
                                     "--workspace", str(workspace), *map(str, args)],
                                    cwd=base, env=env, capture_output=True, text=True, timeout=90)
            if result.returncode != expected_exit:
                raise RuntimeError(label + ": " + result.stdout + result.stderr)
            checks.append({"check": label, "exit": result.returncode})
            return result.stdout

        memory_workflow(command, base)
        if options.memory_only:
            print(json.dumps({"status": "PASS", "checks": checks,
                              "scope": "Public memory CLI with temporary user state"}, indent=2))
            return

        command("help", "--help")
        doctor = json.loads(command("doctor", "doctor"))
        assert doctor["status"] == "READY", doctor
        demo = json.loads(command("demo", "machinery", "demo"))
        assert demo.get("state") == "COMPLETED", demo
        assert demo["result"]["combined_verdict"] == "PASS", demo
        assert Path(demo["result"]["artifact"]).is_file(), demo
        command("conscience help", "conscience", "--help")
        command("wake", "wake")

        evidence = base / "continuity-sources"
        evidence.mkdir()
        (evidence / "original.txt").write_bytes(b"Keep the original evidence.\n")
        (evidence / "addition.txt").write_bytes(b"An additional observation.\n")
        (evidence / "replacement.txt").write_bytes(b"A different evidence source.\n")

        def checkpoint(label, filename, sources, unresolved=True):
            path = base / filename
            args = ["continuity", "create", "--root", evidence, "--output", path,
                    "--task", "Synthetic continuity check", "--status", "RECORDED",
                    "--next-action", "Inspect the retained evidence", "--note", "Synthetic fixture only"]
            for source in sources:
                args.extend(("--source", source))
            if unresolved:
                args.extend(("--unresolved", "Interpretation remains unverified"))
            result = json.loads(command(label, *args))
            # Reopen the emitted file rather than merely trusting its reported pin.
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert result["status"] == "CREATED" and result["sha256"] == digest, result
            return path, digest

        original, original_pin = checkpoint("continuity create", "checkpoint-01.json", ["original.txt"])
        verified = json.loads(command("continuity pinned verify", "continuity", "verify", original,
                                      "--root", evidence, "--expected-sha256", original_pin))
        assert verified["status"] == "VERIFIED" and verified["external_pin_checked"], verified

        def preserve(label, candidate, pin, status):
            result = json.loads(command(label, "continuity", "preserve", original, candidate,
                                        "--root", evidence, "--predecessor-sha256", original_pin,
                                        "--candidate-sha256", pin,
                                        expected_exit=0 if status == "PRESERVED" else 1))
            assert result["status"] == status, result
            return result

        added, added_pin = checkpoint("continuity create addition", "checkpoint-02.json",
                                      ["original.txt", "addition.txt"])
        preserve("continuity preserve addition", added, added_pin, "PRESERVED")
        replaced, replaced_pin = checkpoint("continuity create substitution", "checkpoint-substitution.json",
                                            ["replacement.txt"])
        loss = preserve("continuity reject same-count substitution", replaced, replaced_pin, "LOSS")
        assert any(row["kind"] == "SOURCE_IDENTITY_NOT_RETAINED" for row in loss["losses"]), loss
        dropped, dropped_pin = checkpoint("continuity create omitted unresolved", "checkpoint-drop.json",
                                          ["original.txt"], unresolved=False)
        loss = preserve("continuity reject dropped unresolved", dropped, dropped_pin, "LOSS")
        assert any(row["kind"] == "TEXT_NOT_RETAINED" and row["field"] == "unresolved"
                   for row in loss["losses"]), loss
        (evidence / "addition.txt").write_bytes(b"Changed after checkpoint creation.\n")
        preserve("continuity detect added-source drift", added, added_pin, "DRIFT")

        if os.name == "nt":
            checks.append({"check": "archive round trip", "status": "SKIPPED",
                           "reason": "Secure directory-FD archive operations require macOS/Linux"})
        else:
            source = base / "source"
            source.mkdir()
            data = b"An explicit source and an exact recovered copy.\n"
            (source / "note.txt").write_bytes(data)
            archive = base / "archive"
            command("archive pack", "yggdrasil", "archive", "pack", "--root", "sample=" + str(source),
                    "--destination", archive, "--epoch", "smoke", "--authority-ref", "local-test")
            command("archive verify", "yggdrasil", "archive", "verify", "--archive", archive)
            restored = base / "restored"
            command("archive restore", "yggdrasil", "archive", "restore", "--archive", archive,
                    "--destination", restored)
            assert (restored / "sample/note.txt").read_bytes() == data
            checks.append({"check": "restored file bytes", "sha256": hashlib.sha256(data).hexdigest()})
            assert (source / "note.txt").read_bytes() == data
    print(json.dumps({"status": "PASS", "checks": checks,
                      "scope": "Temporary user state; no provider, scientific run or service launch"}, indent=2))


if __name__ == "__main__":
    main()

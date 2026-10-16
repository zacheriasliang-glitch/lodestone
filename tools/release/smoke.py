#!/usr/bin/env python3
"""Exercise public entry points with temporary user state, without a model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="lodestone-new-user-") as temp:
        base = Path(temp).resolve()
        home = base / "home"
        home.mkdir()
        workspace = base / "workspace"
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "PYTHONDONTWRITEBYTECODE": "1"}

        def command(label, *args):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "lodestone.py"),
                                     "--workspace", str(workspace), *map(str, args)],
                                    cwd=base, env=env, capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise RuntimeError(label + ": " + result.stdout + result.stderr)
            checks.append({"check": label, "exit": result.returncode})
            return result.stdout

        command("help", "--help")
        doctor = json.loads(command("doctor", "doctor"))
        assert doctor["status"] == "READY", doctor
        demo = json.loads(command("demo", "machinery", "demo"))
        assert demo.get("state") == "COMPLETED", demo
        assert demo["result"]["combined_verdict"] == "PASS", demo
        assert Path(demo["result"]["artifact"]).is_file(), demo
        command("conscience help", "conscience", "--help")
        command("wake", "wake")
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

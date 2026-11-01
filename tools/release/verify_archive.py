"""Fresh extraction and offline public-interface checks for one built release ZIP."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory outside source")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output must be new; never reuse prior evidence")
    args.output.mkdir()
    with zipfile.ZipFile(args.archive) as archive:
        names = archive.namelist()
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Unsafe release archive path")
        roots = {PurePosixPath(name).parts[0] for name in names}
        if len(roots) != 1 or len(set(names)) != len(names):
            raise ValueError("Expected one unique release tree")
        archive.extractall(args.output)
    root = args.output / roots.pop()
    user = args.output / "fresh-home"
    user.mkdir()
    env = {**os.environ, "HOME": str(user.resolve()), "USERPROFILE": str(user.resolve()),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    env.pop("PYTHONPATH", None)
    checks = []
    for label, script, options in (
        ("download manifest", "verify_download.py", []),
        ("existing public commands", "tools/release/smoke.py", []),
        *(([("five toolkit operations and stopped recovery", "tools/release/nurarihyon_smoke.py",
              ["--output", str((args.output / "synthetic-workflow").resolve())])])
          if sys.platform in ("darwin", "linux") else []),
    ):
        result = subprocess.run([sys.executable, "-B", str((root / script).resolve()), *options],
                                cwd=user, env=env, capture_output=True, text=True, timeout=600)
        (args.output / (script.split("/")[-1] + ".stdout.txt")).write_text(result.stdout)
        (args.output / (script.split("/")[-1] + ".stderr.txt")).write_text(result.stderr)
        checks.append({"check": label, "exit": result.returncode})
        if result.returncode:
            raise RuntimeError(label + " failed; saved stdout/stderr retain the evidence")
    if sys.platform in ("darwin", "linux"):
        install = subprocess.run([sys.executable, "-B", str((root / "tools/ecosystem/install.py").resolve()),
                                  "--bin-dir", str((user / "bin").resolve())],
                                 cwd=user, env=env, capture_output=True, text=True, timeout=30)
        if install.returncode:
            raise RuntimeError("Explicit launcher installation failed: " + install.stderr)
        launched = subprocess.run([str((user / "bin/lodestone").resolve()), "doctor"],
                                  cwd=user, env=env, capture_output=True, text=True, timeout=30)
        if launched.returncode or json.loads(launched.stdout)["status"] != "READY":
            raise RuntimeError("Installed explicit launcher doctor failed")
        checks.append({"check": "explicit fresh-user launcher install and doctor", "exit": 0})
    report = {"status": "PASS", "archive": args.archive.name,
              "sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
              "bytes": args.archive.stat().st_size,
              "version": (root / "VERSION").read_text().strip(), "checks": checks,
              "git_checkout_required": False, "service_installed": False,
              "platform": sys.platform, "python": sys.version.split()[0]}
    (args.output / "RESULT.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build deterministic downloads from committed public files, never a working tree."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory outside the checkout")
    args = parser.parse_args()
    revision = git("rev-parse", "HEAD").decode().strip()
    if git("diff", revision, "--name-only").strip():
        raise SystemExit("Commit tracked changes before building a release")
    destination = args.output.absolute()
    destination = destination.parent.resolve() / destination.name
    if destination == ROOT or ROOT in destination.parents or destination.exists():
        raise SystemExit("Output must be a new directory outside the checkout")
    version = git("show", revision + ":VERSION").decode().strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", version):
        raise SystemExit("Invalid release version")
    files = {}
    for entry in git("ls-tree", "-rz", revision).split(b"\0"):
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode("utf-8")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise SystemExit("Only ordinary committed files are allowed: " + name)
        if name == "CONTENT_SHA256.json":
            continue
        files[name] = (git("cat-file", "blob", oid), int(mode, 8) & 0o777)
    checksums = {name: hashlib.sha256(data).hexdigest() for name, (data, _) in sorted(files.items())}
    files["CONTENT_SHA256.json"] = ((json.dumps(checksums, indent=2, sort_keys=True) + "\n").encode(), 0o644)
    destination.mkdir(mode=0o755)
    stem = "lodestone-" + version
    with (destination / (stem + ".zip")).open("xb") as raw:
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, (data, mode) in sorted(files.items()):
                info = zipfile.ZipInfo(stem + "/" + name, (1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with (destination / (stem + ".tar.gz")).open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, (data, mode) in sorted(files.items()):
                    item = tarfile.TarInfo(stem + "/" + name)
                    item.size, item.mode, item.mtime = len(data), mode, 0
                    item.uid = item.gid = 0
                    item.uname = item.gname = ""
                    archive.addfile(item, io.BytesIO(data))
    outputs = []
    for path in sorted(destination.iterdir()):
        outputs.append({"name": path.name, "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (destination / "SHA256SUMS.txt").write_text("".join(x["sha256"] + "  " + x["name"] + "\n" for x in outputs))
    report = {"version": version, "commit": revision,
              "files": len(files), "assets": outputs}
    (destination / "BUILD.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

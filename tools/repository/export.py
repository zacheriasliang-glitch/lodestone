#!/usr/bin/env python3
"""Create a private Git backup and an allowlisted clean source archive.

The manifest is a reviewed input, not an automatic privacy classifier.
No network calls, source edits, history edits, or repository publication.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


# The caller supplies an explicit reviewed path allowlist; there is no host manifest.
MARKERS = re.compile(r"/Users/[^/]+/|/home/[^/]+/|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I)


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    if result.returncode:
        raise RuntimeError("git " + args[0] + " failed: " + result.stderr.decode("utf-8", "replace")[:2000])
    return result.stdout


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid export path")
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value
            or ":" in value or path.parts[0] == "CONTENT_SHA256.json"
            or any(p in (".", "..", ".git") or p.startswith(".") for p in path.parts)):
        raise ValueError("unsafe export path: " + value)
    return value


def validate_file_tree(names):
    """All archive members are files: no member may be another file's parent."""
    names = set(names)
    for name in names:
        if any(str(parent) in names for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ValueError("file/directory path collision: " + name)


def inventory(root, commit):
    result = {}
    for entry in git(root, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            mode, kind, oid = meta.decode("ascii").split()
            result[path.decode("utf-8")] = (mode, kind, oid)
    return result


def clean_payload(root, manifest):
    if manifest.get("version") != 1:
        raise ValueError("unsupported manifest version")
    commit = manifest["source_commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source_commit must be a full Git SHA-1")
    observed = git(root, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
    if observed != commit:
        raise ValueError("source commit mismatch")
    entries = inventory(root, commit)
    payload, mapping = {}, []
    if not manifest["files"]:
        raise ValueError("empty source allowlist")
    for item in manifest["files"]:
        path = safe_path(item["path"])
        if path in payload:
            raise ValueError("duplicate source: " + path)
        expected = (item["mode"], "blob", item["blob_sha"])
        if item["mode"] not in ("100644", "100755") or entries.get(path) != expected:
            raise ValueError("source identity/type mismatch: " + path)
        raw = git(root, "cat-file", "blob", item["blob_sha"])
        text = raw.decode("utf-8")
        for replacement in item.get("replacements", []):
            old, new, count = replacement["old"], replacement["new"], replacement["count"]
            if not old or type(count) is not int or count < 1 or text.count(old) != count:
                raise ValueError("replacement precondition failed: " + path)
            text = text.replace(old, new)
        data = text.encode("utf-8")
        payload[path] = (data, int(item["mode"], 8) & 0o777)
        mapping.append({"path": path, "source_blob": item["blob_sha"],
                        "source_sha256": sha256(raw), "export_sha256": sha256(data)})
    for path, content in manifest.get("generated", {}).items():
        if safe_path(path) in payload:
            raise ValueError("generated path collides with selected source: " + path)
        payload[safe_path(path)] = (content.encode("utf-8"), 0o644)
    for path, (data, _) in payload.items():
        if MARKERS.search(data.decode("utf-8")):
            raise ValueError("personal marker requires review: " + path)
        if path.endswith(".py") or path == "tools/machinery/run":
            compile(data, path, "exec")
    validate_file_tree(set(payload) | {"CONTENT_SHA256.json"})
    checksums = {path: sha256(data) for path, (data, _) in sorted(payload.items())}
    payload["CONTENT_SHA256.json"] = (
        (json.dumps(checksums, indent=2, sort_keys=True) + "\n").encode(), 0o644)
    return payload, mapping


def write_archive(path, payload):
    with path.open("xb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, (data, mode) in sorted(payload.items()):
                    item = tarfile.TarInfo(name)
                    item.size, item.mode, item.mtime = len(data), mode, 0
                    item.uid = item.gid = 0
                    item.uname = item.gname = ""
                    archive.addfile(item, io.BytesIO(data))


def export(root, manifest, output):
    root = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("Incomplete history: a shallow checkout cannot produce a complete backup; use a full checkout")
    output = Path(output).absolute()
    # Resolve the parent, but never follow or replace the destination itself.
    output = output.parent.resolve() / output.name
    if output == root or root in output.parents:
        raise ValueError("output must be outside the source checkout")
    if output.exists() or output.is_symlink():
        raise ValueError("output already exists")
    if not output.parent.is_dir():
        raise ValueError("output parent must already exist")
    before = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    payload, mapping = clean_payload(root, manifest)
    refs = git(root, "show-ref").decode()
    # Exclusive directory creation protects any pre-existing destination.
    output.mkdir(mode=0o700)
    bundle = output / "private-repository.bundle"
    git(root, "bundle", "create", str(bundle), "--all", "HEAD")
    git(root, "bundle", "verify", str(bundle))
    archive = output / "clean-harness.tar.gz"
    write_archive(archive, payload)
    after = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
    if before != after or git(root, "rev-parse", "HEAD").decode().strip() != head:
        raise RuntimeError("checkout changed during export; candidate is incomplete")
    if refs != git(root, "show-ref").decode():
        raise RuntimeError("refs changed during export; candidate is incomplete")
    state = {
        "status": "EXPORTED_NOT_PUBLISHED",
        "private_head": head,
        "private_refs": refs.splitlines(),
        "coverage": "Git objects reachable from local refs and HEAD, including committed reasoning/state. "
                    "Untracked, ignored, unsaved files and uncommitted runtime stores are NOT backed up. "
                    "External LFS objects and submodule repositories are NOT backed up.",
        "source_status_porcelain_hex": before.hex(),
        "source_checkout_unchanged": True,
        "clean_source_commit": manifest["source_commit"],
        "clean_manifest_sha256": sha256(json.dumps(manifest, sort_keys=True).encode()),
        "clean_payload_files": len(payload),
        "mapping": mapping,
        "bundle_sha256": sha256(bundle.read_bytes()),
        "clean_archive_sha256": sha256(archive.read_bytes()),
        "privacy_boundary": "Explicit reviewed allowlist plus bounded marker check; not a general privacy proof. "
                            "Clean archive has no Git history. Private bundle and this mapping remain private.",
    }
    with (output / "private-state.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True, help="Explicit reviewed manifest; no inherited repository or history")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        state = export(args.repository, manifest, args.output)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}))
        return 2
    print(json.dumps({key: state[key] for key in (
        "status", "private_head", "clean_source_commit", "clean_payload_files",
        "bundle_sha256", "clean_archive_sha256", "source_checkout_unchanged")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

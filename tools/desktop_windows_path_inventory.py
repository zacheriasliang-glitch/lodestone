#!/usr/bin/env python3
"""Derive Windows path budgets from pinned archives/wheels; never execute inputs."""
import argparse
import configparser
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

WIDTHS = {"lock_sha256":64, "release_id":64, "sha256":64, "stage_suffix":8,
          "uuid":32, "time_ns":20, "uv_archive_id":21, "uv_url_digest":16,
          "random":5, "transaction":25}


def units(text):
    return len(text.encode("utf-16-le")) // 2


def expanded(template):
    return template.format(**{name:"x" * size for name, size in WIDTHS.items()})


def safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Unsafe archive member: " + name)
    return str(path)


def verified(path, artifact):
    if path.stat().st_size != artifact["size"]:
        raise ValueError("Pinned input size differs: " + str(path))
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != artifact["sha256"]:
            raise ValueError("Pinned input hash differs: " + str(path))


def members(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return [safe_member(info.orig_filename) for info in archive.infolist() if not info.is_dir()]
    with tarfile.open(path, "r:gz") as archive:
        return [safe_member(info.name) for info in archive if info.isfile() or info.issym()]


def summarize(files):
    files = {"\\" + name.replace("/", "\\") for name in files}
    directories = {name.rpartition("\\")[0] for name in files}
    longest_file = max(files, key=lambda p:(units(expanded(p)), p))
    longest_directory = max(directories, key=lambda p:(units(expanded(p)), p))
    return {"max_file_relative_utf16":units(expanded(longest_file)),
            "max_directory_relative_utf16":units(expanded(longest_directory)),
            "longest_file_template":longest_file, "longest_directory_template":longest_directory,
            "max_component_utf16":max(units(part) for path in files for part in expanded(path).split("\\"))}


def inventory(payload, artifacts, wheels):
    raw = (payload / "dependencies.windows-x86_64.lock.json").read_bytes()
    lock = json.loads(raw)
    if lock["platform"]["system"] != "Windows":
        raise ValueError("Windows pins are required")
    prefix = "deps/sets/{lock_sha256}/"
    paths = {"root":[], "bootstrap_root":[], "cache":["{sha256}.partial"], "payload":[]}
    sources = {"archives":{}, "wheels":{}}
    for name, artifact in lock["artifacts"].items():
        archive = artifacts / artifact["filename"]
        verified(archive, artifact)
        names = members(archive)
        destination = "python/" + artifact["version"] if name.startswith("python_") else name
        for member in names:
            paths["root"].extend((prefix + destination + "/" + member,
                prefix + destination + ".partial-{stage_suffix}/" + member))
            if name == "python_worker":
                for stage in ("python-{random}-{random}-{random}", "python.{uuid}"):
                    paths["bootstrap_root"].append("deps/bootstrap/" + stage + "/" + member)
        sources["archives"][name] = {"sha256":artifact["sha256"], "size":artifact["size"],
            "filename":artifact["filename"], "full_hash_verified":True, "files":len(names),
            "member_inventory_sha256":hashlib.sha256(json.dumps(sorted(names)).encode()).hexdigest(),
            "archive_paths":summarize(names)}
    for role, environment in lock["environments"].items():
        python_version = ".".join(lock["artifacts"][environment["python"]]["version"].split(".")[:2])
        for wheel in environment["wheels"]:
            archive = wheels / wheel["sha256"] / wheel["filename"]
            verified(archive, wheel)
            names = members(archive)
            installed = []
            for member in names:
                parts = member.split("/")
                if len(parts) > 2 and parts[0].endswith(".data"):
                    category, relative = parts[1], "/".join(parts[2:])
                    base = {"purelib":"Lib/site-packages/", "platlib":"Lib/site-packages/",
                            "scripts":"Scripts/", "data":"",
                            "headers":"include/site/python" + python_version + "/" + wheel["name"] + "/"}.get(category)
                    if base is None:
                        raise ValueError("Unknown wheel install scheme")
                    installed.append(base + relative)
                    # uv first links the complete wheel into site-packages,
                    # then relocates .data members into their install schemes.
                    installed.append("Lib/site-packages/" + member)
                else:
                    installed.append("Lib/site-packages/" + member)
                paths["root"].append(prefix + "uv-cache/archive-v0/{uv_archive_id}/" + member)
            with zipfile.ZipFile(archive) as zipped:
                for name in zipped.namelist():
                    if name.endswith(".dist-info/entry_points.txt"):
                        entries = configparser.ConfigParser()
                        entries.read_string(zipped.read(name).decode("utf-8"))
                        for section in ("console_scripts", "gui_scripts"):
                            if entries.has_section(section):
                                for script in entries[section]:
                                    installed.extend(("Scripts/" + script + ".exe", "Scripts/" + script + "-script.py"))
            for member in installed:
                paths["root"].extend((prefix + role + "/" + member,
                    prefix + role + ".interrupted-{time_ns}/" + member))
            paths["root"].append(prefix + "wheels/{sha256}/" + wheel["filename"] + ".partial-{stage_suffix}")
            tag = wheel["filename"].removesuffix(".whl").split("-", 1)[1]
            paths["root"].append(prefix + "uv-cache/wheels-v5/url/{uv_url_digest}/" + wheel["name"] + "/" + tag + ".rev")
            sources["wheels"][role + "/" + wheel["name"]] = {"sha256":wheel["sha256"], "size":wheel["size"],
                "filename":wheel["filename"], "full_hash_verified":True, "files":len(names),
                "member_inventory_sha256":hashlib.sha256(json.dumps(sorted(names)).encode()).hexdigest(),
                "archive_paths":summarize(names)}
    for path in payload.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            relative = path.relative_to(payload).as_posix()
            paths["payload"].append(relative)
            paths["root"].append("releases/{release_id}/" + relative)
    paths["payload"].extend(("MANIFEST.json", "windows-path-budget.json", "windows_preflight.py", "Enable Windows Long Paths.cmd"))
    for asset in lock["tokenizer"]["files"]:
        paths["root"].append(prefix + "tokenizer/" + asset["filename"] + ".partial-{stage_suffix}")
    paths["root"].extend((prefix + "ollama-models/blobs/sha256-{sha256}.partial-{stage_suffix}",
        prefix + "ollama-models/" + lock["model"]["manifest_path"] + ".partial-{stage_suffix}",
        "transactions/.{transaction}.json.{stage_suffix}", "learning/selection-history/{uuid}/selection.json",
        "Loom/runs/loom-{transaction}-{uuid}/supervisor.json"))
    paths["root"].extend(paths["bootstrap_root"])
    result = {"schema":"lodestone-windows-path-budget/v1", "lock_sha256":hashlib.sha256(raw).hexdigest(),
        "token_widths":WIDTHS, "scopes":{name:summarize(values) for name, values in paths.items()},
        "inputs":sources, "scope":"Pinned bootstrap/install/retained interrupted environments; user-chosen document names are not bounded here.",
        "uv_source_references":[
            "https://github.com/astral-sh/uv/blob/0.9.13/crates/uv-cache/src/archive.rs",
            "https://github.com/astral-sh/uv/blob/0.9.13/crates/uv-distribution/src/distribution_database.rs",
            "https://github.com/astral-sh/uv/blob/0.9.13/crates/uv-install-wheel/src/wheel.rs",
            "https://github.com/astral-sh/uv/blob/0.9.13/crates/uv-python/src/interpreter.rs"],
        "conventions":["All relative budgets include exactly one leading directory separator.",
            "Archive-provided bytecode is counted; installer-generated bytecode is disabled.",
            "uv0.9.13 archive IDs use21characters; temporary extraction names are shorter. Python tempfile suffixes use8characters.",
            "Chosen app filenames and atomic temporary names are checked separately."]}
    root = "C:\\Users\\" + "u" * 20 + "\\AppData\\Local\\Lodestone Preview"
    result["default_root_example"] = {"root":root, "utf16_units":units(root),
        "longest_managed_file_utf16":units(root) + result["scopes"]["root"]["max_file_relative_utf16"],
        "longest_bootstrap_file_utf16":units(root) + result["scopes"]["bootstrap_root"]["max_file_relative_utf16"]}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("payload", "artifacts", "wheels", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = inventory(args.payload, args.artifacts, args.wheels)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"scopes":result["scopes"], "default_root_example":result["default_root_example"]}, indent=2))

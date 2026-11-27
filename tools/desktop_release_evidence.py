#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Lodestone contributors
# License terms: ../LICENSE
"""Offline release evidence assembly; never runs a model or imports product code.

Pass each native job's recovered evidence, its BUILD.json and exact release ZIP.
The BUILD.json directory must also contain its exact native SHA256SUMS.txt.
The caller obtains job URLs/evidence from GitHub; this checks their local byte
bindings and receipts, not a cryptographic signature of GitHub's execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

TARGETS = {"macos-x86_64": ("macOS", "dependencies.lock.json", "installed_app_wrapper"),
           "ubuntu-24.04-x86_64": ("Linux", "dependencies.ubuntu-24.04-x86_64.lock.json", "desktop_exec_command"),
           "windows-x86_64": ("Windows", "dependencies.windows-x86_64.lock.json", "installed_cmd_wrapper")}
MAX_FILE, MAX_EVIDENCE, MAX_PAYLOAD = 8 * 1024**2, 32 * 1024**2, 128 * 1024**2


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def document(raw):
    def invalid(value):
        raise ValueError("Nonfinite JSON value: " + value)
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    need(type(value) is dict, "Expected a JSON object")
    return value


def name(value):
    need(type(value) is str and value and "\\" not in value and ":" not in value
         and "\x00" not in value, "Invalid relative path")
    path = PurePosixPath(value)
    need(not path.is_absolute() and ".." not in path.parts and str(path) == value, "Noncanonical relative path")
    return value


def file_bytes(path, maximum=MAX_FILE):
    path = Path(path)
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
         "Expected a regular, non-reparse file: " + str(path))
    need(info.st_size <= maximum, "File exceeds bound: " + str(path))
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    need(len(raw) == info.st_size and len(raw) <= maximum, "File changed or exceeds bound")
    return raw


def recovered(root):
    root = Path(root).resolve()
    manifest_raw = file_bytes(root / "EVIDENCE-MANIFEST.json")
    manifest = document(manifest_raw)
    need(manifest.get("schema") == "lodestone-ci-log-files/v1", "Unknown recovered evidence format")
    entries = manifest.get("files")
    need(type(entries) is dict and 0 < len(entries) <= 4096, "Invalid evidence inventory")
    actual = set()
    for item in root.rglob("*"):
        info = item.lstat()
        need(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
             "Evidence contains a link or reparse point")
        if not stat.S_ISDIR(info.st_mode):
            actual.add(item.relative_to(root).as_posix())
    need(actual == set(entries) | {"EVIDENCE-MANIFEST.json"}, "Unlisted or missing evidence files")
    result, total = {}, 0
    for relative, identity in entries.items():
        raw = file_bytes(root / name(relative))
        total += len(raw)
        need(total <= MAX_EVIDENCE, "Evidence exceeds total bound")
        need(identity == {"bytes": len(raw), "sha256": sha(raw)}, "Recovered evidence hash mismatch: " + relative)
        result[relative] = raw
    return result, sha(manifest_raw)


def git_source(repository, commit):
    need(re.fullmatch(r"[0-9a-f]{40}", commit), "Expected an exact release commit")
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True,
                            text=True, check=True, timeout=30)
    need(result.stdout.strip() == commit, "Source checkout is not the expected release commit")
    with tempfile.TemporaryFile() as output:
        subprocess.run(["git", "archive", "--format=tar", commit + ":desktop"], cwd=repository,
                       stdout=output, stderr=subprocess.PIPE, check=True, timeout=60)
        need(output.tell() <= MAX_PAYLOAD, "Source snapshot exceeds bound")
        output.seek(0)
        with tarfile.open(fileobj=output, mode="r:") as archive:
            members = archive.getmembers()
            need(len(members) <= 10000, "Source snapshot has too many entries")
            result = {}
            for member in members:
                if member.isdir():
                    continue
                need(member.isfile() and member.size <= MAX_FILE, "Unsupported source snapshot entry")
                result[name(member.name)] = archive.extractfile(member).read(MAX_FILE + 1)
    return result


def payload(archive_path, build, target, source):
    raw = file_bytes(archive_path, MAX_PAYLOAD)
    need(build.get("schema") == "lodestone-desktop-build/v1" and build.get("target") == target,
         "Build target/format differs")
    need(build.get("archive") == Path(archive_path).name and build.get("sha256") == sha(raw)
         and build.get("bytes") == len(raw), "Release ZIP differs from BUILD.json")
    need(build.get("model_weights_bundled") is False and build.get("private_user_records_bundled") is False,
         "Release ZIP scope differs")
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        need(0 < len(members) <= 10000 and sum(m.file_size for m in members) <= MAX_PAYLOAD,
             "Payload expansion exceeds bound")
        values, roots, folded = {}, set(), set()
        for member in members:
            need(member.orig_filename == member.filename, "ZIP name requires normalization")
            relative = name(member.orig_filename)
            parts = PurePosixPath(relative).parts
            need(len(parts) > 1 and relative.casefold() not in folded and member.file_size <= MAX_FILE,
                 "Duplicate, short or oversized ZIP entry")
            need(stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG), "ZIP contains nonregular entries")
            folded.add(relative.casefold())
            roots.add(parts[0])
            values["/".join(parts[1:])] = archive.read(member)
    need(len(roots) == 1 and "MANIFEST.json" in values, "ZIP requires one payload root and manifest")
    manifest_raw = values.pop("MANIFEST.json")
    manifest = document(manifest_raw)
    system, selected_lock, _ = TARGETS[target]
    need(manifest.get("schema") == "lodestone-desktop-payload/v1" and manifest.get("target") == target
         and manifest.get("platform") == system and manifest.get("architecture") == "x86_64",
         "Payload native target differs")
    need(manifest.get("version") == build.get("version"), "Build and payload versions differ")
    need(type(manifest.get("files")) is dict and len(values) == build.get("payload_files")
         and set(values) == set(manifest["files"]), "Payload inventory differs")
    for relative, raw in values.items():
        need(sha(raw) == manifest["files"][name(relative)], "Payload file hash differs: " + relative)
        source_name = selected_lock if relative == "dependencies.lock.json" else relative
        need(source.get(source_name) == raw, "Release ZIP differs from committed source: " + relative)
    need(values.get(selected_lock) == values.get("dependencies.lock.json") and selected_lock in values,
         "Payload default lock differs from target lock")
    release_id = sha(json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    return manifest, sha(manifest_raw), release_id


def native_path(target, value):
    need(type(value) is str and value, "Missing native path")
    path = PureWindowsPath(value) if target == "windows-x86_64" else PurePosixPath(value)
    need(path.is_absolute() and ".." not in path.parts, "Native path is not absolute/canonical")
    return path


def check_target(target, evidence_root, build_path, archive_path, job_url, commit, source):
    need(target in TARGETS, "Unknown native target")
    match = re.fullmatch(r"https://github\.com/zacheriasliang-glitch/lodestone/actions/runs/([0-9]+)/job/([0-9]+)", job_url)
    need(match, "Expected an exact public native job URL")
    files, evidence_sha = recovered(evidence_root)
    def read(relative):
        need(relative in files, "Missing evidence: " + relative)
        return document(files[relative])
    def passed(label):
        value = read(label + ".json")
        need(value.get("status") == "PASSED" and value.get("returncode") == 0, "Native step failed: " + label)
        return value
    report = read("portable-ci.json")
    need(report.get("schema") == "lodestone-portable-ci/v1" and report.get("status") == "PASSED"
         and report.get("target") == target and report.get("source_commit") == commit,
         "Native run did not pass for the exact target/source")
    need(report.get("mode") == "local_inference_smoke" and report.get("model_smoke_invoked") is True,
         "A successful explicit local-inference run is required")
    need(report.get("paid_api_calls") == 0 and report.get("weight_updates") == 0
         and report.get("semantic_learning") == "NOT_ESTABLISHED", "Native run scope differs")
    checks = read("source-checks.json")
    need(checks.get("status") == "PASSED" and checks.get("failed") == [] and checks.get("passed"),
         "Native source checks failed or are missing")
    for label in checks["passed"]:
        need(type(label) is str and label.startswith("source-"), "Invalid source check name")
        passed(label)
    for label in ("build", "fresh-install", "gui-save-reopen", "gui-startup-1", "gui-startup-2",
                  "local-smoke-command", "idempotent-install", "synthetic-upgrade", "rollback", "restored-doctor", "uninstall"):
        passed(label)
    build_raw = file_bytes(build_path)
    need(files.get("download/BUILD.json") == build_raw, "Supplied BUILD.json differs from retained native bytes")
    build = document(build_raw)
    archive_raw = file_bytes(archive_path, MAX_PAYLOAD)
    need(files.get("download/" + name(build["archive"])) == archive_raw,
         "Supplied release ZIP differs from retained native bytes or is missing")
    sidecar_raw = file_bytes(Path(build_path).with_name("SHA256SUMS.txt"))
    need(files.get("download/SHA256SUMS.txt") == sidecar_raw
         and sidecar_raw.decode("utf-8").splitlines() == [build["sha256"] + "  " + build["archive"]],
         "Supplied checksum sidecar differs from retained native bytes or ZIP identity")
    need(read("download-identity.json") == build, "Released BUILD.json differs from native tested download")
    manifest, manifest_sha, release_id = payload(archive_path, build, target, source)
    identity, active = read("current-installed-identity.json"), read("installed-active.json")
    need(identity.get("schema") == "lodestone-mechanical-installed-identity/v1"
         and identity.get("status") == "SOURCE_DOWNLOAD_AND_INSTALLED_BYTES_MATCH"
         and identity.get("source_commit") == commit and identity.get("target") == target
         and identity.get("release_id") == release_id and identity.get("manifest_sha256") == manifest_sha
         and identity.get("files_verified") == len(manifest["files"])
         and identity.get("version") == build["version"], "Source/download/installed identity differs")
    need(active.get("schema") == "lodestone-desktop-active/v1" and active.get("release_id") == release_id
         and active.get("version") == build["version"], "Installed active identity differs")
    installed = native_path(target, active["release"])
    preview = installed.parent.parent
    need(installed == preview / "releases" / release_id, "Installed release path differs")
    gui = read("installed-gui-check.json")
    need(gui.get("schema") == "lodestone-installed-gui-check/v1" and gui.get("status") == "PASSED"
         and type(gui.get("tests")) is int and gui["tests"] > 0
         and gui.get("failures") == 0 and gui.get("errors") == 0 and gui.get("skipped") == []
         and native_path(target, gui.get("payload")) == installed, "Installed GUI suite failed or is unbound")
    if target == "ubuntu-24.04-x86_64":
        passed("native-gui-prerequisites")
        prerequisites = read("linux-gui-prerequisites/installed-gui-check.json")
        need(prerequisites.get("schema") == "lodestone-installed-gui-check/v1"
             and prerequisites.get("status") == "PASSED"
             and type(prerequisites.get("tests")) is int and prerequisites["tests"] >= 2
             and prerequisites.get("failures") == 0 and prerequisites.get("errors") == 0
             and prerequisites.get("skipped") == []
             and native_path(target, prerequisites.get("payload")) == installed,
             "Linux native GUI prerequisites failed or are unbound")
    if target == "windows-x86_64":
        passed("windows-long-path-policy-command")
        policy = read("windows-long-path-policy.json")
        original, restored = policy.get("original_policy"), policy.get("restored_policy")
        zero, one = policy.get("policy_zero", {}), policy.get("policy_one", {})
        need(policy.get("schema") == "lodestone-windows-long-path-policy/v1"
             and policy.get("status") == "PASSED" and policy.get("source_commit") == commit
             and policy.get("release_id") == release_id,
             "Windows prerequisite proof failed or refers to another source/release")
        need(type(original) is dict and set(original) == {"present", "type", "value"}
             and type(original["present"]) is bool and restored == original
             and policy.get("registry_restored") is True,
             "Windows prerequisite test did not restore the original registry value")
        need(type(zero.get("effective_value")) is int and zero["effective_value"] == 0
             and zero.get("short_root_install") is True and zero.get("doctor_ready") is True
             and zero.get("long_root_refused_before_mutation") is True
             and one.get("helper_explicit_yes") is True and type(one.get("effective_value")) is int
             and one["effective_value"] == 1 and one.get("deep_doctor_ready") is True
             and one.get("long_root_preflight_passed") is True,
             "Windows prerequisite refusal, installation or explicit helper proof is missing")
    for number, method in ((1, "direct_gui_python"), (2, TARGETS[target][2])):
        startup = read("gui-startup-receipt-" + str(number) + ".json")
        receipt = startup.get("receipt", {})
        need(startup.get("launch_method") == method and receipt.get("schema") == "lodestone-desktop-startup/v1"
             and receipt.get("release_id") == release_id and receipt.get("shown") is True
             and receipt.get("models") and receipt.get("model_calls") == 0,
             "Installed GUI startup evidence differs")
    lifecycle, retained = read("lifecycle.json"), read("retained-before-lifecycle.json")
    need(lifecycle.get("status") == "PASSED" and lifecycle.get("uninstalled") is True
         and lifecycle.get("original_release") == release_id and lifecycle.get("restored_release") == release_id
         and re.fullmatch(r"[0-9a-f]{64}", lifecycle.get("synthetic_upgrade_release", ""))
         and lifecycle["synthetic_upgrade_release"] != release_id
         and len(retained) > 0 and lifecycle.get("retained_files_verified") == len(retained), "Lifecycle evidence differs")
    for relative, expected in retained.items():
        need(relative.startswith(("learning/", "Loom/data/", "Loom/runs/")), "Retention path is outside native user state")
        need("native/" + name(relative) in files and sha(files["native/" + relative]) == expected,
             "Native retained bytes differ: " + relative)
    smoke = read("local-smoke.json")
    need(smoke.get("schema") == "lodestone-desktop-local-smoke/v1" and smoke.get("status") == "PASSED"
         and smoke.get("release_id") == release_id and native_path(target, smoke.get("root")) == preview
         and smoke.get("paid_api_calls") == 0 and smoke.get("weight_updates") == 0
         and smoke.get("semantic_learning") == "NOT_ESTABLISHED" and smoke.get("max_encounters") == 2
         and smoke.get("max_tokens_per_encounter") == 512,
         "Actual local smoke scope or installation binding differs")
    need(smoke.get("doctor", {}).get("status") == "READY" and smoke["doctor"].get("release_id") == release_id,
         "Smoke installation doctor did not pass")
    attempts = smoke.get("attempts")
    need(type(attempts) is list and len(attempts) == 2, "Two actual smoke encounters are required")
    correction, delivery = smoke.get("correction", {}), smoke.get("delivery", {})
    text = correction.get("text")
    need(type(text) is str and text and correction.get("sha256") == sha(text.encode())
         and correction.get("receipt", {}).get("state") == "RETAINED_UNRESOLVED", "Exact unresolved correction is missing")
    need(delivery.get("exact_correction_in_second_request") is True
         and delivery.get("exact_correction_in_rendered_prompt") is True
         and delivery.get("semantic_use_assessed") is False, "Correction delivery evidence failed")
    run_summaries, seen_runs = [], set()
    for index, attempt in enumerate(attempts, 1):
        receipt, verification = attempt.get("receipt", {}), attempt.get("verification", {})
        need(attempt.get("encounter") == index and receipt.get("status") == "COMPLETED"
             and receipt.get("provider_called") is True and receipt.get("outcome_handle") and receipt.get("terminal_handle"),
             "Native model encounter did not complete")
        run = native_path(target, receipt.get("artifacts_dir"))
        relative = run.relative_to(preview / "Loom/runs")
        need(len(relative.parts) == 1 and str(relative) not in seen_runs, "Run escaped native tree or was reused")
        seen_runs.add(str(relative))
        prefix = "native/Loom/runs/" + name(relative.parts[0]) + "/"
        actual_files = {p[len(prefix):]: sha(raw) for p, raw in files.items() if p.startswith(prefix)}
        need(actual_files and all("/" not in p for p in actual_files)
             and verification.get("files") == actual_files, "Native run bytes differ from smoke verification")
        need(native_path(target, verification.get("artifacts_dir")) == run, "Verification refers to another run")
        worker, generated, supervised = (read(prefix + p) for p in ("worker.json", "result.json", "supervisor.json"))
        config, request = read(prefix + "config.json"), read(prefix + "request.json")
        need(read(prefix + "receipt.json") == receipt, "Smoke receipt differs from the original native receipt")
        observation = generated.get("observation", {})
        supervision = supervised.get("observation", {}).get("supervisor", {})
        need(generated.get("status") == "COMPLETED" and supervised.get("status") == "COMPLETED"
             and type(receipt.get("response")) is str and bool(receipt["response"].strip())
             and generated.get("response") == supervised.get("response") == receipt["response"], "Native model result differs")
        need(verification.get("response_sha256") == sha(receipt["response"].encode())
             and verification.get("worker_pid") == worker.get("pid") and type(worker.get("pid")) is int,
             "Actual worker identity/response differs")
        need(config.get("backend") == worker.get("backend") == "ollama_qwen3_base"
             and native_path(target, config.get("python")) == native_path(target, active.get("worker_python")),
             "Worker interpreter/backend differs from installed selection")
        need(type(config.get("max_rss_bytes")) is int and 0 < config["max_rss_bytes"] <= 4294967296
             and type(config.get("wall_seconds")) in (int, float) and 0 < config["wall_seconds"] <= 1200
             and type(config.get("generation", {}).get("max_tokens")) is int
             and 0 < config["generation"]["max_tokens"] <= 512, "Native model execution bounds differ")
        need(observation.get("source") == "actual supervised local Loom worker"
             and observation.get("endpoint") == "http://127.0.0.1:11435"
             and observation.get("model_digest") == config.get("ollama_model_digest") == verification.get("model_digest")
             and observation.get("token_count_matches") is True and observation.get("done_reason") == "stop"
             and observation.get("paid_api_calls") == 0 and observation.get("weight_updates") == 0,
             "Actual local model generation proof differs")
        need(supervision.get("stop", {}).get("confirmed_stopped") is True
             and supervision.get("rss_valid_sample_count", 0) > 0
             and supervision.get("observed_group_peak_rss_bytes", 0) > 0, "Owned worker cleanup/RSS proof is missing")
        if target == "windows-x86_64":
            need(worker.get("ownership") == "windows_job" and worker.get("filesystem_network_sandbox") is False
                 and supervision.get("windows_job_commit_limit_bytes") == config.get("max_rss_bytes")
                 and supervision.get("windows_job_memory_limit_notified") is False
                 and supervision.get("windows_job_member_evidence_truncated") is False,
                 "Windows Job cap/ownership proof differs")
            members = supervision.get("windows_job_sampled_members", [])
            image_sha = sha(ntpath.normcase(ntpath.normpath(config["ollama_binary"])).encode())
            pids = {row["pid"] for row in members}
            ollama = {row["pid"] for row in members if row.get("image_path_sha256") == image_sha}
            server = observation.get("server_pid")
            runners = ollama - {worker["pid"], server}
            need(0 < len(members) <= 64 and worker["pid"] in pids and server in ollama and runners,
                 "Actual Windows server/runner ownership is missing")
            job = verification.get("windows_job", {})
            need(job.get("sampled_runner_pids") == sorted(runners) and job.get("server_pid") == server
                 and job.get("confirmed_stopped") is True, "Smoke Job proof differs from native sampling")
        need(verification.get("rendered_prompt_sha256") == observation.get("rendered_prompt_sha256"), "Prompt binding differs")
        if index == 2:
            need(text in request.get("context", "") and delivery.get("rendered_prompt_sha256") == observation.get("rendered_prompt_sha256"),
                 "Correction is absent from the bound second native request")
        run_summaries.append({"run": relative.parts[0], "worker_pid": worker["pid"],
                              "model_digest": observation["model_digest"], "response_sha256": verification["response_sha256"],
                              "rendered_prompt_sha256": observation["rendered_prompt_sha256"], "confirmed_stopped": True,
                              "rss_peak_bytes": supervision["observed_group_peak_rss_bytes"],
                              "memory_bound_bytes": config["max_rss_bytes"], "wall_bound_seconds": config["wall_seconds"],
                              "max_tokens": config["generation"]["max_tokens"]})
    return {"target": target, "job_url": job_url, "run_id": match[1], "job_id": match[2],
            "asset": build["archive"], "asset_sha256": build["sha256"], "asset_bytes": build["bytes"],
            "native_zip_retained": True, "checksum_sidecar_sha256": sha(sidecar_raw),
            "version": build["version"], "release_id": release_id, "manifest_sha256": manifest_sha,
            "evidence_manifest_sha256": evidence_sha, "payload_files_verified": len(manifest["files"]),
            "source_checks_passed": len(checks["passed"]), "gui_tests_passed": gui["tests"],
            "retained_files_verified": len(retained), "runs": run_summaries,
            "correction_sha256": correction["sha256"], "exact_later_correction_delivery": True}


def assemble(commit, source, bundles):
    need(re.fullmatch(r"[0-9a-f]{40}", commit), "Expected an exact release commit")
    need(len(bundles) == 3 and {item[0] for item in bundles} == set(TARGETS), "Exactly one bundle for each native target is required")
    targets = [check_target(*bundle, commit, source) for bundle in sorted(bundles)]
    need(len({item["run_id"] for item in targets}) == 1 and len({item["job_id"] for item in targets}) == 3,
         "Native jobs must be distinct members of one exact manual workflow run")
    need(len({item["version"] for item in targets}) == 1, "Native release versions differ")
    return {"schema": "lodestone-release-native-evidence/v1", "status": "VERIFIED_RELEASE_EVIDENCE",
            "source_commit": commit, "version": targets[0]["version"], "targets": targets,
            "targets_verified": 3, "actual_local_encounters": 6, "paid_api_calls": 0, "weight_updates": 0,
            "semantic_learning": "NOT_ESTABLISHED",
            "scope": "Exact committed source and release ZIPs, recovered native installation/GUI/lifecycle receipts, bounded local generation and retained correction delivery. No semantic improvement or trained-weight claim; Windows Job ownership is not filesystem/network confinement."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--target", action="append", nargs=5, required=True,
                        metavar=("TARGET", "EVIDENCE_DIR", "BUILD_JSON", "ZIP", "JOB_URL"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = assemble(args.expected_commit, git_source(args.source, args.expected_commit), args.target)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")
    print(json.dumps({"status": result["status"], "output": str(args.output), "targets_verified": 3}))


if __name__ == "__main__":
    main()

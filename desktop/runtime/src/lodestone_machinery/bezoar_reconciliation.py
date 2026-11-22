"""Bounded captured-corpus reconciliation through the existing organ interfaces.

Trusted callers supply exact capsule bytes and an independently retained hash.
The capsule declares the complete receipt corpus for this operation, not the
contents of an unobserved repository. All declared receipt predecessors and
Origami source bindings must be present. No host source path is reopened.

The caller owns quiescent private scratch and record directories. Evaluation
writes only a fresh explicit scratch directory. Producer and reader
both recompute Yggdrasil findings; saved lineage is comparison data, not proof.
Records retain input bytes and the implementation identity. No authority,
repair, execution, learning or registry-state transition is granted here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat

from . import bezoar_registry as registry
from . import yggdrasil
from .records import canonical

SOURCE_SCHEMA = "bezoar-reconciliation-source/v1"
RESULT_SCHEMA = "bezoar-reconciliation/v1"
MAX_INPUT_BYTES = 65536
MAX_FILES = 64
MAX_RECEIPTS = 32
MAX_ORIGAMI_RESULTS = 8
MAX_RECORD_BYTES = 262144
SHA = re.compile(r"[0-9a-f]{64}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse(raw, limit):
    require(type(raw) is bytes and len(raw) <= limit, "Bounded JSON bytes required")
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "Duplicate JSON field")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("Nonfinite JSON value")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid JSON") from exc
    require(type(value) is dict, "JSON object required")
    return value


def safe_relative(value):
    require(type(value) is str and 0 < len(value) <= 256, "Bounded relative path required")
    require(not value.startswith("/") and not any(p in ("", ".", "..") for p in value.split("/"))
            and not any(c in value for c in ("\\", ":", ","))
            and not any(ord(c) < 32 for c in value), "Unsafe or unmatchable source path")
    return value


def plain(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts
            and not any(p.is_symlink() for p in (path, *path.parents)), "Absolute unaliased path required")
    return path


def implementation():
    """Root-independent pins for this fixed producer and its computation."""
    root = Path(__file__).resolve().parent
    result = {}
    for name in ("bezoar_reconciliation.py", "bezoar_registry.py", "bezoar.py", "yggdrasil.py", "records.py"):
        path = plain(root / name)
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_RECORD_BYTES, "Bounded source file required")
        raw = path.read_bytes()
        require(len(raw) == info.st_size and len(raw) <= MAX_RECORD_BYTES, "Source changed or exceeds bound")
        result[name] = digest(raw)
    return result


def capsule(raw, expected_sha256):
    require(type(expected_sha256) is str and SHA.fullmatch(expected_sha256), "Exact external input hash required")
    require(type(raw) is bytes and len(raw) <= MAX_INPUT_BYTES, "Bounded capsule bytes required")
    require(digest(raw) == expected_sha256, "Input identity differs")
    value = parse(raw, MAX_INPUT_BYTES)
    require(set(value) == {"schema", "corpus", "receipt_inventory", "files", "origami_results", "saved_lineage"}
            and value["schema"] == SOURCE_SCHEMA, "Unsupported reconciliation capsule")
    require(type(value["corpus"]) is str and 1 <= len(value["corpus"]) <= 128, "Explicit captured corpus label required")
    files = value["files"]
    require(type(files) is dict and 2 <= len(files) <= MAX_FILES and "docs/bezoar.md" in files, "Bounded complete captured files required")
    contents = {}
    for name, item in files.items():
        safe_relative(name)
        require(type(item) is dict and set(item) == {"text", "sha256"}
                and type(item["text"]) is str, "Exact UTF-8 source record required")
        data = item["text"].encode("utf-8")
        require(type(item["sha256"]) is str and SHA.fullmatch(item["sha256"])
                and digest(data) == item["sha256"], "Captured source hash differs")
        contents[name] = data
    receipts = sorted(name for name in files if name.startswith("receipts/"))
    require(1 <= len(receipts) <= MAX_RECEIPTS and value["receipt_inventory"] == receipts,
            "Receipt inventory must exactly name the entire captured receipt corpus")
    # Refuse a file whose path is an ancestor of another file before writing.
    for name in files:
        require(not any(parent.as_posix() in files for parent in Path(name).parents if parent.as_posix() != "."),
                "Conflicting captured file paths")
    selected = value["origami_results"]
    require(type(selected) is list and len(selected) <= MAX_ORIGAMI_RESULTS
            and all(type(name) is str for name in selected)
            and len(set(selected)) == len(selected), "Bounded distinct Origami result paths required")
    originals = []
    for name in selected:
        require(type(name) is str and name in contents and not name.startswith("receipts/")
                and name != "docs/bezoar.md", "Captured Origami result missing or misclassified")
        result = parse(contents[name], MAX_INPUT_BYTES)
        require(result.get("schema_version") in ("0.1.0", "0.2.0")
                and type(result.get("status")) is str and "fold" in result
                and (result["fold"] is None or type(result["fold"]) is dict), "Unsupported Origami saved result")
        scope = result.get("scope", {})
        require(type(scope) is dict and type(scope.get("sources", [])) is list, "Invalid Origami source scope")
        for binding in scope.get("sources", []):
            require(type(binding) is dict and type(binding.get("path")) is str
                    and binding["path"] in contents
                    and digest(contents[binding["path"]]) == binding.get("sha256"),
                    "Origami evidence closure missing or changed")
        originals.append({"path": name, "sha256": files[name]["sha256"], "result": result})
    require(value["saved_lineage"] is None or type(value["saved_lineage"]) is dict,
            "Saved lineage must be an optional comparison object")
    return value, contents, originals


def evaluate(raw, *, expected_sha256, scratch):
    """Recompute over a new captured root; retain scratch even on failure."""
    value, contents, originals = capsule(raw, expected_sha256)
    identity = implementation()
    root = plain(scratch)
    require(not root.exists() and root.parent.is_dir(), "Fresh scratch directory required")
    root.mkdir(mode=0o700)
    for name, data in sorted(contents.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    # Existing parser and four-law verifier; do not trust a saved graph verdict.
    report = yggdrasil.check(root)
    require(report["lifecycles"] > 0, "At least one recognized lifecycle receipt required")
    require(not report["absent_declared_entries"], "Receipt predecessor closure incomplete; preserve captured inputs")
    observed = {}
    total = 0
    for index, path in enumerate(root.rglob("*")):
        require(index < 512, "Captured entry bound exceeded")
        require(not path.is_symlink(), "Captured scratch alias refused")
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            continue
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_INPUT_BYTES,
                "Captured ordinary-file bound exceeded")
        with path.open("rb") as stream:
            data = stream.read(MAX_INPUT_BYTES + 1)
        total += len(data)
        require(total <= MAX_INPUT_BYTES and len(observed) < MAX_FILES, "Captured byte/file bound exceeded")
        observed[path.relative_to(root).as_posix()] = data
    require(observed == contents, "Captured corpus changed during verification")
    lines = registry.lines_from_report(report)
    findings = registry.parse_findings("\n".join(lines))
    nodes, duplicates = registry.parse_registry(contents["docs/bezoar.md"].decode("utf-8"))
    issues = registry.check(nodes, duplicates, findings)
    matches = [{"law": law, "subject": subject,
                "holders": [{"id": node.id, "kind": node.kind, "state": node.state}
                            for node in nodes if node.covers(law, subject)]}
               for law, subject in sorted(set(findings))]
    require(implementation() == identity, "Implementation changed during reconciliation")
    result = {"corpus": value["corpus"], "input_sha256": expected_sha256,
              "receipt_inventory": value["receipt_inventory"], "implementation": identity,
              "coverage": "Entire declared captured corpus; no claim about omitted external repositories",
              "closure": "All declared receipt predecessors and Origami source bindings present",
              "lineage": report, "finding_lines": lines, "holder_matches": matches,
              "registry_findings": [{"law": item.law, "where": item.where, "message": item.message} for item in issues],
              "registry_verdict": "ATTENTION" if issues else "PASS",
              "saved_lineage_matches": None if value["saved_lineage"] is None else value["saved_lineage"] == report,
              "origami_results": originals, "origami_semantics": "NOT_EVALUATED; producer outcomes retained unchanged",
              "authority": "NONE", "activation": "NONE", "repair_performed": False,
              "registry_states_changed": False, "learning": False}
    require(len(canonical(result)) <= MAX_RECORD_BYTES, "Reconciliation output exceeds bound")
    return result


def publish_reconciliation(store, raw, *, expected_sha256, scratch):
    """Publish exact input and computed result through the existing RecordStore."""
    scratch = plain(scratch)
    workspace = plain(store.workspace)
    require(scratch != workspace and scratch not in workspace.parents and workspace not in scratch.parents,
            "Scratch and record storage must be separate")
    report = evaluate(raw, expected_sha256=expected_sha256, scratch=scratch)
    source = store.publish("bezoar", "reconciliation-source", {
        "schema": SOURCE_SCHEMA, "sha256": expected_sha256, "raw_utf8": raw.decode("utf-8"), "authority": "NONE"})
    payload = {"schema": RESULT_SCHEMA, "source": source, "report": report, "authority": "NONE"}
    require(len(canonical(payload)) <= MAX_RECORD_BYTES, "Result record exceeds bound")
    return store.publish("bezoar", "reconciliation", payload, links=[source])


def read_reconciliation(store, handle, *, scratch):
    """Reopen stored bytes and independently rerun the fixed computation."""
    scratch, workspace = plain(scratch), plain(store.workspace)
    require(scratch != workspace and scratch not in workspace.parents and workspace not in scratch.parents,
            "Scratch and record storage must be separate")
    def read_record(identity):
        require(store.path(identity).stat().st_size <= MAX_RECORD_BYTES, "Reconciliation record exceeds bound")
        return store.read(identity)
    record = read_record(handle)
    require(record["owner"] == "bezoar" and record["kind"] == "reconciliation", "Wrong producer record type")
    payload = record["payload"]
    require(set(payload) == {"schema", "source", "report", "authority"}
            and payload["schema"] == RESULT_SCHEMA and payload["authority"] == "NONE"
            and record["links"] == [payload["source"]], "Invalid reconciliation record")
    source = read_record(payload["source"])
    require(source["owner"] == "bezoar" and source["kind"] == "reconciliation-source"
            and source["links"] == [], "Invalid reconciliation source record")
    evidence = source["payload"]
    require(set(evidence) == {"schema", "sha256", "raw_utf8", "authority"}
            and evidence["schema"] == SOURCE_SCHEMA and evidence["authority"] == "NONE"
            and type(evidence["raw_utf8"]) is str, "Invalid source evidence")
    require(type(payload["report"]) is dict and payload["report"].get("implementation") == implementation(),
            "Stored computation version differs; no automatic reinterpretation")
    raw = evidence["raw_utf8"].encode("utf-8")
    result = evaluate(raw, expected_sha256=evidence["sha256"], scratch=scratch)
    require(result == payload["report"], "Stored reconciliation differs from recomputed evidence")
    return {"handle": handle, "source": payload["source"], "report": result,
            "verification": "RECOMPUTED_FROM_RETAINED_BYTES", "authority": "NONE", "activation": "NONE"}

"""Authenticated captured evidence, never ambient filesystem access or authority.

Strict complete-capture and explicitly selected historical observation are distinct
contracts. Both leave the inline reconciliation v1 bounds unchanged.
The manifest is small; exact UTF-8 bytes live in immutable RecordStore records.
Every dependency is authenticated before publication. Producer and consumer
both run the existing Yggdrasil/Bezoar calculation. This is engineering, not
learned inference, semantic adjudication, permission, or a repair operation.
"""
from pathlib import Path
import stat
import tempfile

from . import bezoar_reconciliation as r
from .records import RecordStore, canonical, HANDLE

SCHEMA = "bezoar-reconciliation-capture/v1"
FILE_SCHEMA = "bezoar-capture-file/v1"
GROUP_SCHEMA = "bezoar-capture-group/v1"
RESULT_SCHEMA = "bezoar-capture-reconciliation/v1"
OBSERVATION_SCHEMA = "bezoar-reconciliation-observation-source/v1"
OBSERVATION_RESULT_SCHEMA = "bezoar-capture-observation/v1"
OBSERVATION_SUMMARY = ("Captured evidence checked; historical declarations retained as unresolved observations. "
                       "No correction applied, repair, activation or learning.")
MAX_FILES = 192
MAX_RECEIPTS = 160
MAX_FILE_BYTES = 524288
MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_RECORD_TOTAL = 5 * 1024 * 1024
MAX_REPORT_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 65536
GROUP_SIZE = 32
MAX_GROUPS = 6
MAX_REACHABLE = MAX_FILES + MAX_GROUPS + 2  # 200, below RecordStore's256.
STAGE_LIMIT = 16 * 1024 * 1024
# Report + source + EAT records, plan, SQLite growth and bounded log allowance.
STAGE_OVERHEAD = 2 * 1024 * 1024


# Diagnostic-only observations. Explicit worker enable; no data field or host
# environment can enable tracing, authorize an operation or change a result.
_COST_TRACE_PID = None
_COST_TRACE_COUNT = 0
COST_TRACE_SCHEMA = "nurarihyon-cost-phase/v1"
COST_TRACE_PHASES = ("worker_resolve", "worker_copy", "ingestion_eat", "publish_resolve",
    "materialize", "yggdrasil", "reread", "registry", "source_publish", "result_publish",
    "final_board_verify")
COST_TRACE_MAX_EVENTS = 22
COST_TRACE_MAX_EVENT_BYTES = 512


def enable_worker_cost_trace():
    global _COST_TRACE_PID, _COST_TRACE_COUNT
    import os
    _COST_TRACE_PID, _COST_TRACE_COUNT = os.getpid(), 0


def _cost_phase(phase, event):
    import os
    import time
    global _COST_TRACE_COUNT
    if _COST_TRACE_PID != os.getpid():
        return
    if phase not in COST_TRACE_PHASES or event not in ("BEGIN", "END") or _COST_TRACE_COUNT >= COST_TRACE_MAX_EVENTS:
        raise ValueError("Fixed diagnostic trace contract exceeded")
    raw = canonical({"schema": COST_TRACE_SCHEMA, "phase": phase, "event": event,
                     "cpu_seconds": time.process_time(), "wall_seconds": time.monotonic()}) + b"\n"
    if len(raw) > COST_TRACE_MAX_EVENT_BYTES:
        raise ValueError("Fixed diagnostic event exceeds byte bound")
    _COST_TRACE_COUNT += 1
    if os.write(2, raw) != len(raw):
        raise OSError("Incomplete diagnostic stderr write")


def identity():
    pins = r.implementation()
    pins["bezoar_capture.py"] = r.digest(Path(__file__).read_bytes())
    return pins


def sibling(source):
    """One fixed sibling of the explicit caller-selected JSON; no input path field."""
    source = r.plain(Path(source).absolute())
    r.require(source.suffix == ".json", "Capture source must have .json suffix")
    return source.with_suffix(".capture")


def manifest(raw, expected_sha256):
    return _manifest(raw, expected_sha256, observation=False)


def observation_manifest(raw, expected_sha256):
    return _manifest(raw, expected_sha256, observation=True)


def _manifest(raw, expected_sha256, *, observation):
    r.require(type(raw) is bytes and len(raw) <= MAX_MANIFEST_BYTES
              and type(expected_sha256) is str and r.SHA.fullmatch(expected_sha256)
              and r.digest(raw) == expected_sha256, "Capture manifest identity or bound differs")
    value = r.parse(raw, MAX_MANIFEST_BYTES)
    fields = {"schema", "corpus", "files", "receipt_inventory", "origami_results", "saved_lineage", "groups"}
    if observation:
        fields.add("correction_evidence_paths")
    r.require(set(value) == fields and value["schema"] == (OBSERVATION_SCHEMA if observation else SCHEMA),
              "Unsupported captured-evidence manifest contract")
    r.require(type(value["corpus"]) is str and 1 <= len(value["corpus"]) <=128,
              "Explicit captured corpus label required")
    files = value["files"]
    r.require(type(files) is dict and 2 <= len(files) <= MAX_FILES and "docs/bezoar.md" in files,
              "Bounded complete capture required")
    total = 0
    for name, item in files.items():
        r.safe_relative(name)
        r.require(type(item) is dict and set(item) == {"handle", "sha256", "bytes"}
                  and type(item["handle"]) is str and HANDLE.fullmatch(item["handle"])
                  and item["handle"].startswith("bezoar:") and type(item["bytes"]) is int
                  and 0 <= item["bytes"] <= MAX_FILE_BYTES and type(item["sha256"]) is str
                  and r.SHA.fullmatch(item["sha256"]), "Invalid captured file reference")
        total += item["bytes"]
        r.require(not any(p.as_posix() in files for p in Path(name).parents if p.as_posix() != "."),
                  "Conflicting captured file paths")
    r.require(total <= MAX_RAW_BYTES, "Complete capture raw byte bound exceeded")
    receipts = sorted(name for name in files if name.startswith("receipts/"))
    r.require(1 <= len(receipts) <= MAX_RECEIPTS and value["receipt_inventory"] == receipts,
              "Receipt inventory must exactly name the entire captured receipt corpus")
    selected = value["origami_results"]
    r.require(type(selected) is list and len(selected) <= r.MAX_ORIGAMI_RESULTS
              and all(type(x) is str and x in files and x != "docs/bezoar.md"
                      and not x.startswith("receipts/") for x in selected)
              and len(set(selected)) == len(selected), "Invalid selected Origami result inventory")
    r.require(value["saved_lineage"] is None or type(value["saved_lineage"]) is dict,
              "Saved lineage must be comparison data")
    if observation:
        evidence = value["correction_evidence_paths"]
        r.require(type(evidence) is list and len(evidence) <=8
                  and all(type(path) is str and path in receipts for path in evidence)
                  and evidence == sorted(set(evidence)), "Correction evidence must name distinct captured receipts")
    groups = value["groups"]
    r.require(type(groups) is list and 1 <= len(groups) <= MAX_GROUPS
              and all(type(h) is str and HANDLE.fullmatch(h) and h.startswith("bezoar:") for h in groups)
              and groups == sorted(set(groups)), "Invalid capture groups")
    return value


def _record_bytes(record):
    raw = canonical(record)
    r.require(len(raw) <= MAX_RECORD_BYTES, "Capture record bound exceeded")
    return raw


def _plain_record(store, handle):
    path = r.plain(store.path(handle))
    info = path.stat()
    r.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_RECORD_BYTES,
              "Captured record must be bounded ordinary unaliased data")
    return store.read(handle)


class ExistingStore(RecordStore):
    """Use the existing reader without its create-directory constructor."""
    def __init__(self, workspace):
        self.workspace = r.plain(workspace)
        r.require(self.workspace.is_dir(), "Capture dependency directory missing")


class _CaptureCopyStore(RecordStore):
    """Same immutable publisher, with one fresh directory-type observation.

    Only the captured worker's existing destination opts in. No proof is cached
    between traversals or calls. Path/target checks and every inherited record,
    link, hash, canonical-byte, atomic publication and fsync check remain.
    """
    def _directory(self, relative, *, create=False):
        current = self.workspace
        for part in relative.split("/"):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                info = None
            if info is not None and stat.S_ISLNK(info.st_mode):
                raise ValueError("Organ record path contains a symlink")
            if create and (info is None or not stat.S_ISDIR(info.st_mode)):
                # Preserve mkdir's existing regular-file and permission errors.
                # An intervening creator is accepted only after a fresh check.
                current.mkdir(exist_ok=True)
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("Organ record path contains a symlink")
            if info is not None and not stat.S_ISDIR(info.st_mode):
                raise ValueError("Organ record directory is not a directory")
        return current


def worker_copy_store(destination):
    """Explicitly adapt this worker's ordinary writer, never a custom/readonly store."""
    r.require(type(destination) is RecordStore and set(vars(destination)) == {"workspace"},
              "Captured worker copy requires the existing ordinary RecordStore writer")
    workspace = r.plain(destination.workspace)
    r.require(workspace.is_dir(), "Captured worker destination must already exist")
    return _CaptureCopyStore(workspace)


def resolve(raw, store, *, expected_sha256):
    return _resolve(raw, store, expected_sha256=expected_sha256, observation=False)


def resolve_observation(raw, store, *, expected_sha256):
    return _resolve(raw, store, expected_sha256=expected_sha256, observation=True)


def _resolve(raw, store, *, expected_sha256, observation):
    """Authenticate every physical dependency and Origami binding; no writes."""
    value = _manifest(raw, expected_sha256, observation=observation)
    records, members = {}, []
    for handle in value["groups"]:
        rec = _plain_record(store, handle)
        r.require(rec["owner"] == "bezoar" and rec["kind"] == "capture-group"
                  and rec["payload"] == {"schema": GROUP_SCHEMA, "authority": "NONE"}
                  and 1 <= len(rec["links"]) <= GROUP_SIZE,
                  "Invalid captured dependency group")
        records[handle] = rec
        members.extend(rec["links"])
    expected = sorted(set(item["handle"] for item in value["files"].values()))
    r.require(sorted(members) == expected, "Capture group closure differs from manifest")
    contents = {}
    for name, item in value["files"].items():
        handle = item["handle"]
        if handle not in records:
            records[handle] = _plain_record(store, handle)
        rec = records[handle]
        p = rec["payload"]
        r.require(rec["owner"] == "bezoar" and rec["kind"] == "capture-file" and rec["links"] == []
                  and set(p) == {"schema", "raw_utf8", "sha256", "bytes", "authority"}
                  and p["schema"] == FILE_SCHEMA and p["authority"] == "NONE"
                  and type(p["raw_utf8"]) is str and type(p["bytes"]) is int,
                  "Invalid captured file record")
        data = p["raw_utf8"].encode("utf-8")
        r.require(len(data) == p["bytes"] == item["bytes"] and len(data) <= MAX_FILE_BYTES
                  and r.digest(data) == p["sha256"] == item["sha256"], "Captured file identity differs")
        contents[name] = data
    size = sum(len(_record_bytes(rec)) for rec in records.values())
    r.require(size <= MAX_RECORD_TOTAL and len(records) + 2 <= MAX_REACHABLE,
              "Captured dependency graph budget exceeded")
    virtual = {k: v for k, v in value.items() if k != "groups"}
    virtual["schema"] = r.SOURCE_SCHEMA
    virtual["files"] = {name: {"text": data.decode("utf-8"), "sha256": r.digest(data)}
                        for name, data in contents.items()}
    originals = []
    for name in value["origami_results"]:
        result = r.parse(contents[name], MAX_FILE_BYTES)
        r.require(result.get("schema_version") in ("0.1.0", "0.2.0")
                  and type(result.get("status")) is str and "fold" in result
                  and (result["fold"] is None or type(result["fold"]) is dict),
                  "Unsupported Origami saved result")
        scope = result.get("scope", {})
        r.require(type(scope) is dict and type(scope.get("sources", [])) is list, "Invalid Origami source scope")
        for binding in scope.get("sources", []):
            r.require(type(binding) is dict and type(binding.get("path")) is str
                      and binding["path"] in contents
                      and r.digest(contents[binding["path"]]) == binding.get("sha256"),
                      "Origami evidence closure missing or changed")
        originals.append({"path": name, "sha256": value["files"][name]["sha256"], "result": result})
    return {"manifest": value, "value": virtual, "contents": contents, "originals": originals,
            "records": records, "record_bytes": size, "raw_bytes": sum(map(len, contents.values()))}


def evaluate_resolved(capture, *, expected_sha256, scratch):
    return _evaluate_resolved(capture, expected_sha256=expected_sha256, scratch=scratch, observation=False)


def observe_resolved(capture, *, expected_sha256, scratch):
    return _evaluate_resolved(capture, expected_sha256=expected_sha256, scratch=scratch, observation=True)


def _evaluate_resolved(capture, *, expected_sha256, scratch, observation):
    # The v1 producer file remains byte-identical because its strict historical
    # reader pins that file. This wrapper reuses the actual Yggdrasil/registry
    # operations; it does not introduce another lineage or discrepancy algorithm.
    from . import yggdrasil, bezoar_registry as registry
    value, contents, originals = capture["value"], capture["contents"], capture["originals"]
    r.require(capture["manifest"]["schema"] == (OBSERVATION_SCHEMA if observation else SCHEMA),
              "Captured evaluation contract differs")
    pins = identity()
    root = r.plain(scratch)
    r.require(not root.exists() and root.parent.is_dir(), "Fresh scratch directory required")
    _cost_phase("materialize", "BEGIN")
    root.mkdir(mode=0o700)
    for name, data in sorted(contents.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    _cost_phase("materialize", "END")
    _cost_phase("yggdrasil", "BEGIN")
    report = yggdrasil.check(root)
    _cost_phase("yggdrasil", "END")
    r.require(report["lifecycles"] >0, "At least one recognized lifecycle receipt required")
    if not observation:
        r.require(not report["absent_declared_entries"], "Receipt predecessor closure incomplete; preserve captured inputs")
    _cost_phase("reread", "BEGIN")
    observed, total = {}, 0
    for index, path in enumerate(root.rglob("*")):
        r.require(index <512 and not path.is_symlink(), "Captured entry bound or alias violation")
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            continue
        r.require(stat.S_ISREG(info.st_mode) and info.st_nlink ==1 and info.st_size <= MAX_FILE_BYTES,
                  "Captured ordinary-file bound exceeded")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES +1)
        total += len(data)
        r.require(total <=MAX_RAW_BYTES and len(observed) <MAX_FILES, "Captured byte/file bound exceeded")
        observed[path.relative_to(root).as_posix()] = data
    r.require(observed ==contents, "Captured corpus changed during verification")
    _cost_phase("reread", "END")
    _cost_phase("registry", "BEGIN")
    lines = registry.lines_from_report(report)
    findings = registry.parse_findings("\n".join(lines))
    nodes, duplicates = registry.parse_registry(contents["docs/bezoar.md"].decode("utf-8"))
    issues = registry.check(nodes, duplicates, findings)
    matches = [{"law": law, "subject": subject,
                "holders": [{"id": node.id, "kind": node.kind, "state": node.state}
                            for node in nodes if node.covers(law, subject)]}
               for law, subject in sorted(set(findings))]
    r.require(identity() ==pins, "Implementation changed during reconciliation")
    result = {"corpus": value["corpus"], "input_sha256": expected_sha256,
              "receipt_inventory": value["receipt_inventory"], "implementation": pins,
              "coverage": "Entire declared captured corpus; no claim about omitted external repositories",
              "closure": "All declared receipt predecessors and Origami source bindings present",
              "lineage": report, "finding_lines": lines, "holder_matches": matches,
              "registry_findings": [{"law": item.law, "where": item.where, "message": item.message} for item in issues],
              "registry_verdict": "ATTENTION" if issues else "PASS",
              "saved_lineage_matches": None if value["saved_lineage"] is None else value["saved_lineage"] ==report,
              "origami_results": originals, "origami_semantics": "NOT_EVALUATED; producer outcomes retained unchanged",
              "authority": "NONE", "activation": "NONE", "repair_performed": False,
              "registry_states_changed": False, "learning": False}
    if observation:
        absent = report["absent_declared_entries"]
        result["closure"] = "Authenticated captured files and Origami source bindings; historical declarations may be unresolved"
        result.update(
            capture_integrity="COMPLETE", origami_source_bindings="COMPLETE",
            historical_declaration_coverage="INCOMPLETE" if absent else "COMPLETE_FOR_CAPTURED_CORPUS",
            missing_historical_paths=list(absent),
            discrepancy_state="UNRESOLVED" if (absent or any(report["counts"].values()) or issues)
                              else "NONE_REPORTED_IN_CAPTURED_CORPUS",
            correction_evidence=[{"path": path, "handle": capture["manifest"]["files"][path]["handle"],
                                  "sha256": capture["manifest"]["files"][path]["sha256"],
                                  "applied": False, "authority": "NONE"}
                                 for path in capture["manifest"]["correction_evidence_paths"]])
    r.require(len(canonical(result)) <=MAX_REPORT_BYTES, "Reconciliation output exceeds bound")
    _cost_phase("registry", "END")
    return result


def preflight(raw, directory):
    """Verify closure in disposable private scratch before durable state writes.

    This work counts toward any execution budget; it is not a free validation.
    A failure retains the caller's original source package unchanged.
    """
    return _preflight(raw, directory, observation=False)


def preflight_observation(raw, directory):
    """Require physical closure; retain historical absence as typed evidence."""
    return _preflight(raw, directory, observation=True)


def _preflight(raw, directory, *, observation):
    capture = _resolve(raw, ExistingStore(directory), expected_sha256=r.digest(raw), observation=observation)
    with tempfile.TemporaryDirectory(prefix="bezoar-capture-preflight-") as temporary:
        _evaluate_resolved(capture, expected_sha256=r.digest(raw), scratch=Path(temporary).resolve() / "corpus",
                           observation=observation)
    return capture


def stage_requirement(capture, raw, parent_bytes):
    return (parent_bytes + 2 * capture["raw_bytes"] + capture["record_bytes"]
            + 8 * len(raw) + STAGE_OVERHEAD)


def entry_requirement(value, parent_entries):
    """Reserve both materializations, copied graph and fixed stage metadata.

    Parent entries are cloned in full. Source-derived counts can only increase
    the reservation; they cannot alter the sealed filesystem-entry limit.
    """
    directories = {parent.as_posix() for name in value["files"]
                   for parent in Path(name).parents if parent.as_posix() != "."}
    records = len({item["handle"] for item in value["files"].values()}) + len(value["groups"])
    input_entries = records +5  # sibling, organs/owner/records, manifest
    stage_entries = parent_entries + records + 2 * (len(value["files"]) + len(directories) +1) +64
    return {"input": input_entries, "stage": stage_entries}


def copy_records(capture, destination):
    """Copy only authenticated declared bytes, with existing immutable publication."""
    # File records first; group publication verifies every existing child.
    for kind in ("capture-file", "capture-group"):
        for handle, record in sorted(capture["records"].items()):
            if record["kind"] == kind:
                actual = destination.publish("bezoar", kind, record["payload"], links=record["links"])
                r.require(actual == handle, "Copied dependency identity differs")


def _separate(store, scratch):
    root, workspace = r.plain(scratch), r.plain(store.workspace)
    r.require(root != workspace and root not in workspace.parents and workspace not in root.parents,
              "Scratch and record storage must be separate")


def publish(store, raw, *, expected_sha256, scratch):
    return _publish(store, raw, expected_sha256=expected_sha256, scratch=scratch, observation=False)


def publish_observation(store, raw, *, expected_sha256, scratch):
    return _publish(store, raw, expected_sha256=expected_sha256, scratch=scratch, observation=True)


def _publish(store, raw, *, expected_sha256, scratch, observation):
    _separate(store, scratch)
    _cost_phase("publish_resolve", "BEGIN")
    capture = _resolve(raw, store, expected_sha256=expected_sha256, observation=observation)
    _cost_phase("publish_resolve", "END")
    report = _evaluate_resolved(capture, expected_sha256=expected_sha256, scratch=scratch, observation=observation)
    _cost_phase("source_publish", "BEGIN")
    source = store.publish("bezoar", "observation-source" if observation else "capture-source", {
        "schema": OBSERVATION_SCHEMA if observation else SCHEMA,
        "raw_utf8": raw.decode("utf-8"), "sha256": expected_sha256,
        "authority": "NONE"}, links=capture["manifest"]["groups"])
    _cost_phase("source_publish", "END")
    _cost_phase("result_publish", "BEGIN")
    result_handle = store.publish("bezoar", "capture-observation" if observation else "capture-reconciliation", {
        "schema": OBSERVATION_RESULT_SCHEMA if observation else RESULT_SCHEMA,
        "source": source, "report": report, "authority": "NONE"}, links=[source])
    _cost_phase("result_publish", "END")
    return result_handle


def read(store, handle, *, scratch):
    return _read(store, handle, scratch=scratch, observation=False)


def read_observation(store, handle, *, scratch):
    return _read(store, handle, scratch=scratch, observation=True)


def _read(store, handle, *, scratch, observation):
    _separate(store, scratch)
    rec = _plain_record(store, handle)
    p = rec["payload"]
    r.require(rec["owner"] == "bezoar" and rec["kind"] == ("capture-observation" if observation else "capture-reconciliation")
              and set(p) == {"schema", "source", "report", "authority"}
              and p["schema"] == (OBSERVATION_RESULT_SCHEMA if observation else RESULT_SCHEMA) and p["authority"] == "NONE"
              and rec["links"] == [p["source"]], "Invalid complete reconciliation record")
    source = _plain_record(store, p["source"])
    s = source["payload"]
    r.require(source["owner"] == "bezoar" and source["kind"] == ("observation-source" if observation else "capture-source")
              and set(s) == {"schema", "raw_utf8", "sha256", "authority"}
              and s["schema"] == (OBSERVATION_SCHEMA if observation else SCHEMA) and s["authority"] == "NONE" and type(s["raw_utf8"]) is str,
              "Invalid complete source record")
    raw = s["raw_utf8"].encode("utf-8")
    capture = _resolve(raw, store, expected_sha256=s["sha256"], observation=observation)
    r.require(source["links"] == capture["manifest"]["groups"], "Source dependency closure differs")
    r.require(type(p["report"]) is dict and p["report"].get("implementation") == identity(),
              "Captured computation version differs")
    actual = _evaluate_resolved(capture, expected_sha256=s["sha256"], scratch=scratch, observation=observation)
    r.require(actual == p["report"], "Stored captured-evidence reconciliation differs")
    return {"handle": handle, "source": p["source"], "report": actual,
            "verification": "RECOMPUTED_FROM_RETAINED_BYTES", "authority": "NONE", "activation": "NONE",
            "dependencies": sorted(capture["records"])}


def prepare(root, spec, destination):
    return _prepare(root, spec, destination, observation=False)


def prepare_observation(root, spec, destination):
    return _prepare(root, spec, destination, observation=True)


def _prepare(root, spec, destination, *, observation):
    """Explicit CLI capture: declared root + exact path/hash/size pins, no discovery.

    The caller chooses the root and output; neither is read from source content.
    All pinned sources and full closure are checked before publishing the bundle.
    """
    root, destination = r.plain(root), r.plain(destination)
    r.require(root.is_dir() and not destination.exists() and not sibling(destination).exists(),
              "Preparation requires an existing root and unused destinations")
    fields = {"corpus", "files", "receipt_inventory", "origami_results", "saved_lineage"}
    if observation:
        fields.add("correction_evidence_paths")
    r.require(type(spec) is dict and set(spec) == fields, "Invalid capture specification")
    files, records = {}, {}
    r.require(type(spec["files"]) is dict and 2 <= len(spec["files"]) <= MAX_FILES, "Capture file bound")
    total = 0
    for name, pin in sorted(spec["files"].items()):
        r.safe_relative(name)
        r.require(type(pin) is dict and set(pin) == {"sha256", "bytes"}
                  and type(pin["bytes"]) is int and 0 <= pin["bytes"] <= MAX_FILE_BYTES,
                  "Every source requires exact independent size/hash pins")
        path = r.plain(root / name)
        info = path.stat()
        r.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == pin["bytes"],
                  "Captured source must be ordinary, unaliased and exactly pinned")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        total += len(data)
        r.require(total <= MAX_RAW_BYTES and len(data) == pin["bytes"] and r.digest(data) == pin["sha256"],
                  "Captured source identity or total bound differs")
        payload = {"schema": FILE_SCHEMA, "raw_utf8": data.decode("utf-8"), "sha256": pin["sha256"],
                   "bytes": pin["bytes"], "authority": "NONE"}
        rec = {"schema": "organ-record/v1", "owner": "bezoar", "kind": "capture-file",
               "payload": payload, "links": []}
        handle = "bezoar:" + r.digest(_record_bytes(rec))
        records[handle] = rec
        files[name] = {"handle": handle, **pin}
    groups = []
    handles = sorted(records)
    for offset in range(0, len(handles), GROUP_SIZE):
        rec = {"schema": "organ-record/v1", "owner": "bezoar", "kind": "capture-group",
               "payload": {"schema": GROUP_SCHEMA, "authority": "NONE"},
               "links": handles[offset:offset + GROUP_SIZE]}
        handle = "bezoar:" + r.digest(_record_bytes(rec))
        records[handle] = rec
        groups.append(handle)
    raw = canonical({**spec, "schema": OBSERVATION_SCHEMA if observation else SCHEMA,
                     "files": files, "groups": sorted(groups)})
    _manifest(raw, r.digest(raw), observation=observation)
    r.require(sum(len(_record_bytes(rec)) for rec in records.values()) <= MAX_RECORD_TOTAL,
              "Capture serialized dependency budget exceeded")
    with tempfile.TemporaryDirectory(prefix="bezoar-capture-prepare-") as temporary:
        staged = RecordStore(Path(temporary) / "records")
        copy_records({"records": records}, staged)
        capture = _preflight(raw, staged.workspace, observation=observation)
        # No original path is retained/reopened after this immutable capture.
        copy_records(capture, RecordStore(sibling(destination)))
        with destination.open("xb") as stream:
            stream.write(raw)
    return {"source_sha256": r.digest(raw), "manifest_bytes": len(raw), "files": len(files),
            "raw_bytes": total, "records": len(records), "authority": "NONE"}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with args.spec.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    spec = r.parse(raw, MAX_MANIFEST_BYTES)
    print(canonical(prepare(args.root.absolute(), spec, args.output.absolute())).decode("utf-8"))


if __name__ == "__main__":
    main()

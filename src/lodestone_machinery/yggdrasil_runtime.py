"""Active Yggdrasil snapshot and re-entry; reads evidence, never executes it."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from . import macro_voxel as mv
from . import yggdrasil as lineage
from .records import HANDLE, NAME, canonical as record_canonical
from .yggdrasil_sources import binding_payload, read_bound_sources, BoundSourceError
from .support_review import REQUEST_SCHEMA, ATTRIBUTION, _validate_review
from .origami import _validate_response

ORIGAMI_RESULT_VERSIONS = ("0.1.0", "0.2.0")
CANON = "docs/yggdrasil/canon.json"
ADOPTION = "docs/dispositions/HOST_DISPOSITION__YGGDRASIL_ACTIVE_v0.3.0.md"
REQUIRED = {"node.md", "LABORATORY_CONSTITUTION.md", "INTERACTION_PROTOCOL.md",
            "UNIVERSAL_RECEIPT_POLICY_v1.0.0.md", ADOPTION,
            "docs/yggdrasil/macro-voxel-v0.2.0.md"}
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_ROOTS = 64
MAX_SNAPSHOT_SOURCE_BYTES = mv.MAX_BYTES * 3 // 4  # base64 alone cannot fit above this


def read(root, relative, limit=mv.MAX_SOURCE_BYTES):
    """Bounded reads of regular files; no symlinks in a supplied relative path."""
    path = Path(root).resolve()
    for part in mv.safe_path(relative).split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError("Symlink in source: " + relative)
    if not path.is_file():
        raise ValueError("Source unavailable: " + relative)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Source exceeds bound: " + relative)
    return data


def inventory(root, relative):
    """Complete file inventory in a declared subtree, refusing symbolic links."""
    base = Path(root).resolve()
    directory = base / mv.safe_path(relative)
    for part in [directory, *directory.parents]:
        if part == base:
            break
        if part.is_symlink():
            raise ValueError("Symlink in scope: " + relative)
    if not directory.is_dir():
        raise ValueError("Scope unavailable: " + relative)
    result = []
    for current, dirs, files in os.walk(directory, followlinks=False):
        # Interpreter caches are generated output, not runtime source.
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in dirs + files:
            if (Path(current) / name).is_symlink():
                raise ValueError("Symlink in source scope")
        for name in sorted(files):
            result.append((Path(current) / name).relative_to(base).as_posix())
            if len(result) > mv.MAX_NODES:
                raise ValueError("Source inventory exceeds bound")
    return sorted(result)


def check_canon(root):
    manifest = mv.parse_json(read(root, CANON))
    mv._keys(manifest, {"schema", "status", "body_id", "authority", "runtime_authority",
                         "adoption_ref", "bindings", "scope"}, "canon")
    if (manifest["schema"] != "yggdrasil-canon/v1" or manifest["status"] != "ACTIVE"
            or manifest["authority"] != "HOST" or manifest["runtime_authority"] != "NONE"
            or manifest["adoption_ref"] != ADOPTION):
        raise ValueError("Canon is not an active bounded Host adoption")
    mv._text(manifest["body_id"], "canonical body")
    mv._strings(manifest["scope"], "canon scope")
    bindings = manifest["bindings"]
    if type(bindings) is not dict or not REQUIRED <= bindings.keys():
        raise ValueError("Canon omits a required source binding")
    for path, expected in bindings.items():
        if type(expected) is not str or mv.SHA.fullmatch(expected) is None:
            raise ValueError("Invalid canon source identity")
        if mv.digest(read(root, path)) != expected:
            raise ValueError("Stale canon binding: " + path)
    return manifest


def repo_paths(root, canon):
    return sorted(set(canon["bindings"]) | {CANON, "CURRENT_STATE.md"}
                  | set(inventory(root, "receipts"))
                  | set(inventory(root, "src/lodestone_machinery")))


def node(identifier, body, epoch, source_id, role, state, lifecycle_id=None):
    return {"id": identifier, "address": {"node_id": identifier, "body_id": body,
            "lifecycle_id": lifecycle_id, "epoch": epoch, "source_contract": source_id},
            "role": role, "selected_organ": None,
            "phase": {"pulse_id": None, "phase_name": None, "ordinal": None, "direction": None},
            "state": state, "evidence_refs": [source_id],
            "unknowns": ["Execution phase and selected operational organ are not inferred from this source."],
            "contradictions": [], "debt": []}


def edge(source, target, kind, refs):
    return {"source": source, "target": target, "kind": kind,
            "bridge": {"authority": "NONE", "approval_binding": None,
                       "declared_invariants": ["source identity", "typed direction", "retained evidence"],
                       "evidence_refs": refs}, "residue": mv.residue()}


def _record(raw, path):
    record = mv.parse_json(raw)
    mv._keys(record, {"schema", "owner", "kind", "payload", "links"}, "organ record")
    for field in ("owner", "kind"):
        if type(record[field]) is not str or NAME.fullmatch(record[field]) is None:
            raise ValueError("Invalid record " + field)
    if record["schema"] != "organ-record/v1" or type(record["payload"]) is not dict:
        raise ValueError("Invalid organ record schema")
    mv._strings(record["links"], "record links")
    if len(record["links"]) > 32 or any(HANDLE.fullmatch(h) is None for h in record["links"]):
        raise ValueError("Invalid bounded record links")
    canonical = record_canonical(record)
    identity = mv.digest(canonical)
    if path != "organs/" + record["owner"] + "/records/" + identity + ".json" or canonical != raw:
        raise ValueError("Organ record differs from its producer-owned address")
    return record["owner"] + ":" + identity, record


def _selected_paths(workspace, handles):
    if type(handles) not in (tuple, list) or not 1 <= len(handles) <= 256:
        raise ValueError("Select 1 to 256 exact record handles")
    if any(type(h) is not str or HANDLE.fullmatch(h) is None for h in handles) or len(set(handles)) != len(handles):
        raise ValueError("Invalid or duplicate selected record handle")
    pending, seen, paths = list(handles), set(), []
    while pending:
        handle = pending.pop()
        if handle in seen:
            continue
        if len(seen) >= 256:
            raise ValueError("Selected record closure exceeds 256 records")
        owner, digest = handle.split(":")
        path = "organs/" + owner + "/records/" + digest + ".json"
        actual, record = _record(read(workspace, path), path)
        if actual != handle:
            raise ValueError("Selected record identity differs")
        seen.add(handle)
        paths.append(path)
        pending.extend(record["links"])
    return sorted(paths)


def _records(sources):
    result = {}
    for source_id, item in sources.items():
        if item["scope"] == "records" and "/records/" in item["path"] and item["path"].endswith(".json"):
            handle, record = _record(mv.source_bytes(item), item["path"])
            result[handle] = (source_id, record)
    return result


def _capture_roots(value):
    value = {} if value is None else value
    if type(value) is not dict or len(value) > MAX_CAPTURE_ROOTS:
        raise ValueError("Supply at most 64 explicit capture roots")
    for handle, root in value.items():
        if (type(handle) is not str or HANDLE.fullmatch(handle) is None
                or not handle.startswith("origami:") or not isinstance(root, (str, Path)) or not str(root)):
            raise ValueError("Capture roots require exact Origami binding handles and paths")
    return value


def _support_packets(records):
    try:
        return _support_packets_checked(records)
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        raise ValueError("Malformed retained support packet") from exc


def _support_packets_checked(records):
    bindings, answers, reviews = {}, {}, {}
    for handle, (_, record) in records.items():
        if record["owner"] == "origami" and record["kind"] == "source_bindings":
            bindings[handle] = binding_payload(record)
    for handle, (_, record) in records.items():
        if record["owner"] != "origami" or record["kind"] != "answer_evidence":
            continue
        if len(record["links"]) != 1 or record["links"][0] not in bindings:
            raise ValueError("Answer evidence requires its exact source binding")
        packet, bound = record["payload"], bindings[record["links"][0]]
        required = {"schema", "run_id", "question", "epoch", "reviewer", "provider", "files", "source_root",
                    "scope", "fold", "evidence", "prior_knowledge", "instructions", "response_schema",
                    "attribution", "authority", "request_sha256"}
        mv._keys(packet, required, "answer evidence packet")
        for field in ("question", "epoch", "reviewer", "provider", "instructions"):
            mv._text(packet[field], field)
        if (packet["schema"] != REQUEST_SCHEMA or packet["authority"] != "NONE"
                or packet["attribution"] != ATTRIBUTION
                or any(record_canonical(packet[k]) != record_canonical(bound[k])
                       for k in ("run_id", "source_root", "scope", "evidence"))
                or mv.digest(record_canonical({k: v for k, v in packet.items() if k != "request_sha256"})) != packet["request_sha256"]):
            raise ValueError("Answer evidence identity or source binding differs")
        fold = packet["fold"]
        if type(fold) is not dict:
            raise ValueError("Answer evidence requires its exact fold")
        _validate_response(fold, {"request_id": fold.get("request_id"), "evidence": packet["evidence"], "scope": packet["scope"]})
        if fold["decision"] != "ANSWER":
            raise ValueError("Answer evidence must retain an ANSWER fold")
        answers[handle] = (record["links"][0], packet)
    for handle, (_, record) in records.items():
        if record["owner"] != "checker" or record["kind"] != "support_review":
            continue
        if len(record["links"]) != 1 or record["links"][0] not in answers:
            raise ValueError("Support review requires its exact answer record")
        payload, packet = record["payload"], answers[record["links"][0]][1]
        mv._keys(payload, {"request_sha256", "run_id", "review", "attribution", "authority"}, "support review record")
        if (payload["run_id"] != packet["run_id"] or payload["request_sha256"] != packet["request_sha256"]
                or payload["attribution"] != ATTRIBUTION or payload["authority"] != "NONE"):
            raise ValueError("Support review differs from its answer")
        _validate_review(payload["review"], packet)
        reviews[handle] = (record["links"][0], payload["review"])
    return bindings, answers, reviews


def snapshot(root, *, records_workspace=None, origami_results=(), capture_roots=None, record_handles=()):
    root = Path(root).resolve()
    canon = check_canon(root)
    scopes, sources, roots = {}, {}, {"repo": root}
    raw_total = 0

    def capture(name, kind, directory, paths):
        nonlocal raw_total
        scopes[name] = {"kind": kind, "paths": sorted(paths)}
        roots[name] = Path(directory).resolve()
        for path in sorted(paths):
            if raw_total + (Path(directory) / path).stat().st_size > MAX_SNAPSHOT_SOURCE_BYTES:
                raise ValueError("Snapshot sources cannot fit the macro-voxel byte bound")
            raw = read(directory, path)
            raw_total += len(raw)
            if raw_total > MAX_SNAPSHOT_SOURCE_BYTES:
                raise ValueError("Snapshot sources exceed the macro-voxel byte bound")
            sources[name + ":" + path] = mv.source(name, path, raw)

    capture("repo", "repository", root, repo_paths(root, canon))
    if record_handles and records_workspace is None:
        raise ValueError("Selected records require their workspace")
    if records_workspace is not None:
        capture("records", "organ-records-selected" if record_handles else "organ-records", records_workspace,
                _selected_paths(records_workspace, record_handles) if record_handles else inventory(records_workspace, "organs"))
    for index, filename in enumerate(origami_results):
        path = Path(filename).absolute()
        capture("origami-" + str(index), "origami-result", path.parent, [path.name])
    records = _records(sources)
    bindings, answers, reviews = _support_packets(records)
    capture_roots = _capture_roots(capture_roots)
    if set(capture_roots) != set(bindings):
        raise ValueError("Supply exactly one explicit capture root for each source_bindings record")
    declared_bytes = sum(entry["bytes"] for bound in bindings.values() for entry in bound["scope"]["sources"])
    if declared_bytes > MAX_CAPTURE_BYTES or raw_total + declared_bytes > MAX_SNAPSHOT_SOURCE_BYTES:
        raise ValueError("Declared captured sources exceed the aggregate snapshot byte bound")
    used, captured = 0, {}
    for handle, bound in bindings.items():
        checked = read_bound_sources(records[handle][1], capture_roots[handle], max_bytes=MAX_CAPTURE_BYTES - used)
        used += checked["read_bytes"]
        raw_total += checked["read_bytes"]
        if raw_total > MAX_SNAPSHOT_SOURCE_BYTES:
            raise ValueError("Snapshot sources exceed the macro-voxel byte bound")
        name = "capture:" + handle
        scopes[name] = {"kind": "origami-capture", "paths": sorted(checked["sources"])}
        for path, source in checked["sources"].items():
            sources[name + ":" + path] = mv.source(name, path, source["text"].encode("utf-8"))
        captured[handle] = checked
    epoch = mv.digest(mv.canonical({k: v["sha256"] for k, v in sources.items()}))
    body = canon["body_id"]
    nodes = [node(k, body, epoch, k, "source_account", {"source_sha256": v["sha256"]})
             for k, v in sources.items()]
    by_id = {n["id"]: n for n in nodes}
    edges, unresolved = [], []
    # Run lineage against captured bytes, not a second unbound read of live receipts.
    with tempfile.TemporaryDirectory(prefix="yggdrasil-lineage-") as temporary:
        for item in sources.values():
            if item["scope"] == "repo" and item["path"].startswith("receipts/"):
                target = Path(temporary) / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(mv.source_bytes(item))
        (Path(temporary) / "receipts").mkdir(exist_ok=True)
        lifecycles, others = lineage.load(temporary)
        report = lineage.verify(lifecycles, others)
    for entry in lifecycles:
        source_id = "repo:" + entry.path
        current = by_id[source_id]
        current["role"] = "lifecycle_receipt"
        current["state"] = {key: list(getattr(entry, key)) if type(getattr(entry, key)) is tuple
                            else getattr(entry, key) for key in entry.__slots__}
        current["address"]["lifecycle_id"] = entry.receipt_id
        for predecessor in entry.declared or ():
            parent = "repo:" + predecessor
            if parent in by_id:
                edges.append(edge(parent, source_id, "declared_predecessor", [source_id]))
            else:
                unresolved.append({"source": source_id, "missing_predecessor": predecessor})

    handles = {handle: source_id for handle, (source_id, _) in records.items()}
    for handle, (source_id, record) in records.items():
        by_id[source_id]["role"] = "organ_record:" + record["owner"]
        by_id[source_id]["state"] = record
    for handle, source_id in handles.items():
        for dependency in by_id[source_id]["state"]["links"]:
            if HANDLE.fullmatch(dependency) is None:
                raise ValueError("Invalid linked record handle")
            if dependency in handles:
                edges.append(edge(handles[dependency], source_id, "record_dependency", [source_id]))
            else:
                unresolved.append({"source": source_id, "missing_record": dependency})

    passages, answer_claims = {}, {}
    for handle, checked in captured.items():
        binding_id = handles[handle]
        for passage in checked["evidence"]:
            identifier = binding_id + "#passage:" + passage["id"]
            source_id = "capture:" + handle + ":" + passage["source"]
            nodes.append(node(identifier, body, epoch, source_id, "origami_passage", passage))
            passages[(handle, passage["id"])] = identifier
            edges.append(edge(source_id, identifier, "contains_passage", [source_id]))
            edges.append(edge(identifier, binding_id, "bound_passage", [binding_id]))
    for handle, (binding, packet) in answers.items():
        answer_id, claims = handles[handle], {}
        for claim in packet["fold"]["claims"]:
            identifier = answer_id + "#claim:" + claim["id"]
            nodes.append(node(identifier, body, epoch, answer_id, "origami_claim", claim))
            claims[claim["id"]] = identifier
            edges.append(edge(answer_id, identifier, "contains_claim", [answer_id]))
            for evidence in claim["support"]:
                edges.append(edge(passages[(binding, evidence)], identifier, "claim_support", [answer_id]))
        for relation in packet["fold"]["relations"]:
            edges.append(edge(claims[relation["from"]], claims[relation["to"]], "origami:" + relation["type"], [answer_id]))
        answer_claims[handle] = claims
    for handle, (answer, review) in reviews.items():
        review_id = handles[handle]
        for row in review["claim_verdicts"]:
            edges.append(edge(answer_claims[answer][row["id"]], review_id, "claim_review:" + row["verdict"], [review_id]))

    for source_id, item in sources.items():
        if not item["scope"].startswith("origami-"):
            continue
        result = mv.parse_json(mv.source_bytes(item))
        if (type(result) is not dict or result.get("schema_version") not in ORIGAMI_RESULT_VERSIONS
                or "fold" not in result or (result["fold"] is not None and type(result["fold"]) is not dict)):
            raise ValueError("Expected an Origami saved result, schema 0.1.0 or 0.2.0")
        current = by_id[source_id]
        current["role"], current["state"] = "origami_result", result
        fold = result["fold"] or {}
        if result["fold"] is None:
            current["unknowns"].append("Producer returned no semantic fold; retain failure status and reason.")
        for field in ("claims", "relations", "unresolved"):
            if type(fold.get(field, [])) is not list:
                raise ValueError("Invalid Origami fold " + field)
        current["unknowns"].extend(fold.get("unresolved", []))
        scope = result.get("scope", {})
        if type(scope) is not dict or type(scope.get("sources", [])) is not list:
            raise ValueError("Invalid Origami source scope")
        for binding in scope.get("sources", []):
            if type(binding) is not dict or type(binding.get("path")) is not str:
                raise ValueError("Invalid Origami evidence source")
            support = [handle for handle, payload in bindings.items()
                       if payload["run_id"] == result.get("run_id") and payload["scope"] == scope]
            if len(support) > 1:
                raise ValueError("Ambiguous Origami result source bindings")
            prefix = "capture:" + support[0] if support else "repo"
            bound = sources.get(prefix + ":" + binding["path"])
            if bound is None or bound["sha256"] != binding.get("sha256"):
                unresolved.append({"source": source_id, "unverified_fold_source": binding})
            else:
                edges.append(edge(prefix + ":" + binding["path"], source_id, "origami_source", [source_id]))
        claims = {}
        for claim in fold.get("claims", []):
            if type(claim) is not dict:
                raise ValueError("Invalid Origami claim")
            claim_id = mv._text(claim.get("id"), "Origami claim id")
            if claim_id in claims:
                raise ValueError("Duplicate Origami claim id")
            identifier = source_id + "#claim:" + claim_id
            claims[claim_id] = identifier
            child = node(identifier, body, epoch, source_id, "origami_claim", claim)
            nodes.append(child)
            edges.append(edge(source_id, identifier, "contains_claim", [source_id]))
        for relation in fold.get("relations", []):
            if type(relation) is not dict:
                raise ValueError("Invalid Origami relation")
            mv._text(relation.get("from"), "relation source")
            mv._text(relation.get("to"), "relation target")
            if relation.get("from") in claims and relation.get("to") in claims:
                edges.append(edge(claims[relation["from"]], claims[relation["to"]],
                                  "origami:" + mv._text(relation.get("type"), "relation type"), [source_id]))
            else:
                unresolved.append({"source": source_id, "unresolved_relation": relation})
    graph = {"schema": "yggdrasil-graph/v1", "body_id": body, "epoch": epoch,
             "host": {"authority": "EXTERNAL", "approval_inherited": False},
             "scopes": scopes, "sources": sources, "nodes": nodes, "edges": edges,
             "lineage": report, "residue": mv.residue(contradictions=report["findings"], unknowns=unresolved)}
    # Detect a source or inventory change during capture. No stable-snapshot claim if it moved.
    voxel = mv.encode(graph)
    if reenter(voxel, root, records_workspace=records_workspace,
               origami_results=origami_results, capture_roots=capture_roots,
               record_handles=record_handles)["freshness"] != "CURRENT":
        raise ValueError("Sources changed while capturing macro-voxel")
    return voxel


def reenter(voxel, root, *, records_workspace=None, origami_results=(), capture_roots=None, record_handles=()):
    graph = mv.decode(voxel)
    differences, unavailable = [], []
    capture_roots = _capture_roots(capture_roots)
    source_checks, used, records = [], 0, {}
    try:
        records = _records(graph["sources"])
        bindings, _, _ = _support_packets(records)
    except (ValueError, KeyError, TypeError) as exc:
        unavailable.append("Invalid retained support record: " + str(exc))
        bindings = {}
    for extra in set(capture_roots) - set(bindings):
        unavailable.append("Capture root does not match a retained source binding: " + extra)
    for handle, bound in bindings.items():
        scope_name = "capture:" + handle
        status = {"binding": handle, "status": "UNAVAILABLE", "read_bytes": 0}
        if scope_name not in graph["scopes"]:
            status["reason"] = "External source bytes were not captured; rebuild this legacy snapshot"
            unavailable.append(status["reason"] + ": " + handle)
        elif handle not in capture_roots:
            status["reason"] = "Explicit capture root not supplied"
            unavailable.append(status["reason"] + ": " + handle)
        else:
            try:
                checked = read_bound_sources(records[handle][1], capture_roots[handle], max_bytes=MAX_CAPTURE_BYTES - used)
                used += checked["read_bytes"]
                status["read_bytes"] = checked["read_bytes"]
                expected_paths = sorted(checked["sources"])
                if graph["scopes"][scope_name] != {"kind": "origami-capture", "paths": expected_paths}:
                    raise ValueError("Captured source inventory differs from the binding")
                for path, source in checked["sources"].items():
                    item = graph["sources"].get(scope_name + ":" + path)
                    if (item is None or item["scope"] != scope_name or item["path"] != path
                            or item["sha256"] != source["sha256"]):
                        raise ValueError("Captured source differs from bound evidence: " + path)
                status.update(status="CURRENT", sources_checked=len(checked["sources"]), passages_checked=len(checked["evidence"]))
            except BoundSourceError as exc:
                used += exc.read_bytes
                status.update(status="STALE" if exc.code == "SOURCE_DRIFT" else "UNAVAILABLE",
                              reason=str(exc), code=exc.code, source=exc.source,
                              expected_sha256=exc.expected_sha256, observed_sha256=exc.observed_sha256,
                              read_bytes=exc.read_bytes)
                (differences if status["status"] == "STALE" else unavailable).append(str(exc) + ": " + handle)
            except (OSError, ValueError) as exc:
                used += getattr(exc, "read_bytes", 0)
                status.update(reason=str(exc), read_bytes=getattr(exc, "read_bytes", status["read_bytes"]))
                unavailable.append(str(exc) + ": " + handle)
        source_checks.append(status)
    try:
        canon = check_canon(root)
        if canon["body_id"] != graph["body_id"]:
            differences.append("canonical body differs")
        current_repo = repo_paths(root, canon)
    except (ValueError, OSError) as exc:
        differences.append(str(exc))
        current_repo = None
    roots = {"repo": Path(root)}
    current = {"repo": current_repo}
    if records_workspace is not None:
        roots["records"] = Path(records_workspace)
        try:
            selected = graph["scopes"].get("records", {}).get("kind") == "organ-records-selected"
            if selected:
                selected_handles = record_handles or [h for h in records]
                current["records"] = _selected_paths(records_workspace, selected_handles)
            else:
                if record_handles:
                    raise ValueError("Record selection differs from the full-inventory snapshot")
                current["records"] = inventory(records_workspace, "organs")
        except (ValueError, OSError) as exc:
            unavailable.append(str(exc))
    for index, filename in enumerate(origami_results):
        path = Path(filename).absolute()
        name = "origami-" + str(index)
        roots[name], current[name] = path.parent, [path.name]
    for name, scope in graph["scopes"].items():
        if name.startswith("capture:"):
            if scope["kind"] != "origami-capture" or name[8:] not in bindings:
                unavailable.append("Unknown external capture scope: " + name)
            # The binding-specific checks above reopen every declared file and passage.
            continue
        expected_kind = ("repository" if name == "repo" else "organ-records" if name == "records"
                         else "origami-result" if name.startswith("origami-") else None)
        if scope["kind"] != expected_kind and not (name == "records" and scope["kind"] == "organ-records-selected"):
            unavailable.append("Unsupported scope: " + name)
        if name not in roots:
            unavailable.append("Required scope not supplied: " + name)
            continue
        if current.get(name) is not None and current[name] != scope["paths"]:
            differences.append("Source inventory changed: " + name)
    if "repo" not in graph["scopes"]:
        unavailable.append("No repository binding in this graph")
    for item in graph["sources"].values():
        if item["scope"].startswith("capture:"):
            continue
        if item["scope"] not in roots:
            continue
        try:
            if mv.digest(read(roots[item["scope"]], item["path"])) != item["sha256"]:
                differences.append("Source changed: " + item["scope"] + ":" + item["path"])
        except (ValueError, OSError) as exc:
            differences.append(str(exc))
    # Coverage gaps and absent selected organs remain visible rather than becoming approval.
    has_findings = (bool(graph["lineage"].get("findings"))
                    or any(graph["residue"][k] for k in ("unknowns", "contradictions", "debt"))
                    or any(n[k] for n in graph["nodes"] for k in ("unknowns", "contradictions", "debt")))
    freshness = "UNAVAILABLE" if unavailable else "STALE" if differences else "CURRENT"
    action = ("PROVIDE_SOURCES" if unavailable else "REFRESH_SOURCES" if differences
              else "REVIEW_FINDINGS" if has_findings else "READ_BOUND_CONTEXT")
    return {"macro_sha256": voxel["sha256"], "body_id": graph["body_id"],
            "freshness": freshness, "next_action": action, "authority": "NONE",
            "differences": differences, "unavailable": unavailable,
            "lineage": graph["lineage"], "residue": graph["residue"],
            "source_checks": source_checks,
            "freshness_scope": "Captured inputs and explicit external source bindings; not semantic support or reviewer authentication",
            "unresolved_nodes": [n["id"] for n in graph["nodes"]
                                 if any(n[k] for k in ("unknowns", "contradictions", "debt"))]}


def record_anchors(graph):
    """Resolve exact producer handles to nodes whose captured bytes bind them."""
    records = _records(graph["sources"])
    nodes = {item["id"]: item for item in graph["nodes"]}
    result = {}
    for handle, (source_id, record) in records.items():
        current = nodes.get(source_id)
        if (current is None or record_canonical(current["state"]) != record_canonical(record)
                or current["role"] != "organ_record:" + record["owner"]):
            raise ValueError("Captured record node differs from its producer")
        result[handle] = source_id
    return result


def parse_capture_roots(values):
    result = {}
    for value in values:
        handle, separator, path = value.partition("=")
        if not separator or handle in result:
            raise ValueError("Use one --capture-root HANDLE=PATH per source binding")
        result[handle] = path
    return _capture_roots(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("canon", "pack", "inspect", "expand", "reenter"):
        p = commands.add_parser(name)
        if name in ("canon", "pack", "reenter"):
            p.add_argument("--root", default=".")
        if name in ("pack", "reenter"):
            p.add_argument("--records-workspace")
            p.add_argument("--origami-result", action="append", default=[])
            p.add_argument("--capture-root", action="append", default=[], metavar="HANDLE=PATH")
            p.add_argument("--record", action="append", default=[], help="Exact organ handle and its dependency closure")
        if name == "pack":
            p.add_argument("--output", required=True)
        if name in ("inspect", "expand", "reenter"):
            p.add_argument("voxel")
            p.add_argument("--expected-sha256")
        if name == "expand":
            p.add_argument("--anchor", required=True)
            p.add_argument("--depth", type=int, default=1)
            p.add_argument("--direction", choices=("both", "ancestors", "descendants"), default="both")
    args = parser.parse_args(argv)
    try:
        if args.command == "canon":
            result = check_canon(args.root)
        elif args.command == "pack":
            voxel = snapshot(args.root, records_workspace=args.records_workspace, origami_results=args.origami_result,
                             capture_roots=parse_capture_roots(args.capture_root), record_handles=args.record)
            output = Path(args.output).absolute()
            if output.resolve().is_relative_to(Path(args.root).resolve()):
                raise ValueError("Write generated macro-voxels outside the repository")
            # Exclusive creation avoids replacing caller data or following a destination symlink.
            with output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(voxel, ensure_ascii=False, allow_nan=False) + "\n")
            result = {k: v for k, v in voxel.items() if k != "payload"}
            result["output"] = str(output)
        else:
            path = Path(args.voxel).absolute()
            voxel = mv.parse_json(read(path.parent, path.name, mv.MAX_BYTES))
            graph = mv.decode(voxel, expected_sha256=args.expected_sha256)
            if args.command == "inspect":
                result = {"sha256": voxel["sha256"], "body_id": graph["body_id"],
                          "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
                          "scopes": graph["scopes"], "lineage": graph["lineage"],
                          "residue": graph["residue"], "record_anchors": record_anchors(graph), "authority": "NONE"}
            elif args.command == "expand":
                anchor = record_anchors(graph).get(args.anchor, args.anchor)
                result = mv.expand(voxel, anchor, direction=args.direction, depth=args.depth)
            else:
                result = reenter(voxel, args.root, records_workspace=args.records_workspace,
                                 origami_results=args.origami_result, capture_roots=parse_capture_roots(args.capture_root),
                                 record_handles=args.record)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 2 if result.get("freshness") in ("STALE", "UNAVAILABLE") else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2

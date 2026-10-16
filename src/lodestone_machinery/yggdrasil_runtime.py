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

ORIGAMI_RESULT_VERSIONS = ("0.1.0", "0.2.0")
CANON = "docs/yggdrasil/canon.json"
ADOPTION = "docs/dispositions/HOST_DISPOSITION__YGGDRASIL_ACTIVE_v0.3.0.md"
REQUIRED = {"node.md", "LABORATORY_CONSTITUTION.md", "INTERACTION_PROTOCOL.md",
            "UNIVERSAL_RECEIPT_POLICY_v1.0.0.md", ADOPTION,
            "docs/yggdrasil/macro-voxel-v0.2.0.md"}


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


def snapshot(root, *, records_workspace=None, origami_results=()):
    root = Path(root).resolve()
    canon = check_canon(root)
    scopes, sources, roots = {}, {}, {"repo": root}

    def capture(name, kind, directory, paths):
        scopes[name] = {"kind": kind, "paths": sorted(paths)}
        roots[name] = Path(directory).resolve()
        for path in sorted(paths):
            sources[name + ":" + path] = mv.source(name, path, read(directory, path))

    capture("repo", "repository", root, repo_paths(root, canon))
    if records_workspace is not None:
        capture("records", "organ-records", records_workspace,
                inventory(records_workspace, "organs"))
    for index, filename in enumerate(origami_results):
        path = Path(filename).absolute()
        capture("origami-" + str(index), "origami-result", path.parent, [path.name])
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

    handles = {}
    for source_id, item in sources.items():
        if item["scope"] != "records" or "/records/" not in item["path"] or not item["path"].endswith(".json"):
            continue
        record = mv.parse_json(mv.source_bytes(item))
        mv._keys(record, {"schema", "owner", "kind", "payload", "links"}, "organ record")
        for field in ("owner", "kind"):
            if type(record[field]) is not str or NAME.fullmatch(record[field]) is None:
                raise ValueError("Invalid record " + field)
        if record["schema"] != "organ-record/v1" or type(record["payload"]) is not dict:
            raise ValueError("Invalid organ record schema")
        mv._strings(record["links"], "record links")
        identity = mv.digest(record_canonical(record))
        if item["path"] != "organs/" + record["owner"] + "/records/" + identity + ".json":
            raise ValueError("Organ record differs from its producer-owned address")
        handle = record["owner"] + ":" + identity
        handles[handle] = source_id
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
            bound = sources.get("repo:" + binding["path"])
            if bound is None or bound["sha256"] != binding.get("sha256"):
                unresolved.append({"source": source_id, "unverified_fold_source": binding})
            else:
                edges.append(edge("repo:" + binding["path"], source_id, "origami_source", [source_id]))
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
               origami_results=origami_results)["freshness"] != "CURRENT":
        raise ValueError("Sources changed while capturing macro-voxel")
    return voxel


def reenter(voxel, root, *, records_workspace=None, origami_results=()):
    graph = mv.decode(voxel)
    differences, unavailable = [], []
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
            current["records"] = inventory(records_workspace, "organs")
        except (ValueError, OSError) as exc:
            unavailable.append(str(exc))
    for index, filename in enumerate(origami_results):
        path = Path(filename).absolute()
        name = "origami-" + str(index)
        roots[name], current[name] = path.parent, [path.name]
    for name, scope in graph["scopes"].items():
        expected_kind = ("repository" if name == "repo" else "organ-records" if name == "records"
                         else "origami-result" if name.startswith("origami-") else None)
        if scope["kind"] != expected_kind:
            unavailable.append("Unsupported scope: " + name)
        if name not in roots:
            unavailable.append("Required scope not supplied: " + name)
            continue
        if current.get(name) is not None and current[name] != scope["paths"]:
            differences.append("Source inventory changed: " + name)
    if "repo" not in graph["scopes"]:
        unavailable.append("No repository binding in this graph")
    for item in graph["sources"].values():
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
            "unresolved_nodes": [n["id"] for n in graph["nodes"]
                                 if any(n[k] for k in ("unknowns", "contradictions", "debt"))]}


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
            voxel = snapshot(args.root, records_workspace=args.records_workspace, origami_results=args.origami_result)
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
                          "residue": graph["residue"], "authority": "NONE"}
            elif args.command == "expand":
                result = mv.expand(voxel, args.anchor, direction=args.direction, depth=args.depth)
            else:
                result = reenter(voxel, args.root, records_workspace=args.records_workspace,
                                 origami_results=args.origami_result)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 2 if result.get("freshness") in ("STALE", "UNAVAILABLE") else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2

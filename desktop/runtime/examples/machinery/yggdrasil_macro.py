"""Portable lossless macro-voxel and bounded re-entry-context demonstration.

Synthetic evidence only; no private lab or Host approval is bundled.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery import macro_voxel as mv
from lodestone_machinery.yggdrasil_runtime import node, edge


def main():
    source = mv.source("example", "evidence.txt", b"Recorded: 3. Observed: 4. Difference unresolved.\n")
    a = node("record", "example:body", "e1", "evidence", "source_account", {"observed": 3})
    b = node("return", "example:body", "e1", "evidence", "implementation_evidence", {"observed": 4})
    b["contradictions"] = [{"recorded": 3, "observed": 4}]
    b["debt"] = ["Explain the difference"]
    graph = {"schema": "yggdrasil-graph/v1", "body_id": "example:body", "epoch": "e1",
             "host": {"authority": "EXTERNAL", "approval_inherited": False},
             "scopes": {"example": {"kind": "synthetic", "paths": ["evidence.txt"]}},
             "sources": {"evidence": source}, "nodes": [a, b],
             "edges": [edge("record", "return", "contradicts", ["evidence"])],
             "lineage": {"scope": "synthetic example"}, "residue": mv.residue(debt=b["debt"])}
    voxel = mv.encode(graph)
    recovered = mv.decode(json.loads(json.dumps(voxel)), expected_sha256=voxel["sha256"])
    if recovered != graph:
        raise RuntimeError("Projection or source evidence was lost")
    context = mv.expand(voxel, "return", direction="ancestors", depth=1)
    if len(context["nodes"]) != 2 or context["residue"]["debt"] != b["debt"]:
        raise RuntimeError("Consumer lost the contrary evidence or debt")
    print(json.dumps({"round_trip": "PASS", "consumer": "PASS", "authority": "NONE",
                      "nodes": len(context["nodes"]), "raw_bytes": voxel["raw_bytes"],
                      "compressed_bytes": voxel["compressed_bytes"], "debt": context["residue"]["debt"]}))


if __name__ == "__main__":
    main()

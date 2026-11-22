"""Run from the repository root: python3 examples/machinery/custom_worker.py /tmp/lodestone-custom

Produce a real JSON source inventory through the registered Worker/Auditor API.
This is a deterministic extension, not a model invocation or simulated reasoning.
"""
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery import Engine, WorkerRequest, WorkerReturn
from lodestone_machinery.organs import anchor


class SourceInventoryWorker:
    artifact_name = "inventory.json"

    def build(self, request: WorkerRequest) -> WorkerReturn:
        sources = []
        for source in request.folded["sources"]:
            sources.append({"path": source["path"], "bytes": source["bytes"],
                            "sha256": source["sha256"],
                            "selected_passages": sum(p["source"] == source["path"] for p in request.folded["passages"])})
        output = {"sources": sources, "query": request.envelope["query"],
                  "insufficient": request.folded["insufficient"]}
        return WorkerReturn(request.envelope_id, json.dumps(output, indent=2) + "\n",
                            "Source inventory only. Counts and byte identities do not establish semantic coverage.")


class InventoryAuditor:
    def audit(self, root: Path, folded: dict, artifact: str) -> dict:
        findings = []
        try:
            parsed = json.loads(artifact)
            if set(parsed) != {"sources", "query", "insufficient"}:
                raise ValueError("Unexpected inventory fields")
            if parsed["query"] != folded["query"] or parsed["insufficient"] != folded["insufficient"]:
                findings.append("Query or evidence status was changed")
            rows = parsed["sources"]
            if [row["path"] for row in rows] != [source["path"] for source in folded["sources"]]:
                findings.append("Source inventory differs from selected source set")
            for row in rows:
                if set(row) != {"path", "bytes", "sha256", "selected_passages"}:
                    raise ValueError("Unexpected source row fields")
                current = anchor(root, row["path"])
                if row["bytes"] != current["bytes"] or row["sha256"] != current["sha256"]:
                    findings.append("Current source identity differs: " + row["path"])
                valid_intervals = set()
                original_lines = current["text"].splitlines(keepends=True)
                for passage in folded["passages"]:
                    if passage["source"] != row["path"]:
                        continue
                    interval = (passage["start_line"], passage["end_line"])
                    literal = "".join(original_lines[interval[0] - 1:interval[1]])
                    if literal != passage["text"] or hashlib.sha256(literal.encode()).hexdigest() != passage["slice_sha256"]:
                        findings.append("Selected interval does not match original source")
                    valid_intervals.add(interval)
                if type(row["selected_passages"]) is not int or row["selected_passages"] != len(valid_intervals):
                    findings.append("Incorrect selected-passage count: " + row["path"])
        except (ValueError, TypeError, KeyError) as exc:
            findings.append("Invalid inventory: " + str(exc))
        return {"verdict": "FAIL" if findings else "PASS", "findings": findings,
                "scope": "JSON structure, current source identities, exact interval counts; no semantic conclusion"}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 examples/machinery/custom_worker.py WORKSPACE")
    engine = Engine(sys.argv[1])
    engine.register_worker("source_inventory", SourceInventoryWorker())
    engine.register_auditor("inventory_audit", InventoryAuditor())
    root = Path(__file__).resolve().parents[2] / "src/lodestone_machinery/samples"
    job_id = engine.submit(root, ["workshop.md", "repair.md"], "ceramic",
                           worker="source_inventory", auditor="inventory_audit")
    result = engine.run(job_id)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["state"] == "COMPLETED" else 2)

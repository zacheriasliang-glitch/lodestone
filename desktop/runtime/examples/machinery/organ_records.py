"""Run two real jobs and inspect the organ-owned outputs that route them."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery import Engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    engine = Engine(args.workspace)
    root = Path(__file__).resolve().parents[2] / "src/lodestone_machinery/samples"
    outcomes = []
    for query, expected in (("ceramic", "COMPLETED"), ("unfindablequartz", "BLOCKED")):
        job = engine.submit(root, ["workshop.md", "repair.md"], query)
        result = engine.run(job)
        if result["state"] != expected:
            raise RuntimeError("Unexpected job outcome: " + json.dumps(result))
        handles = result["result"]["organ_records"]
        decision = engine.records.read(handles["orchestrator"])["payload"]
        if decision["state"] != result["state"]:
            raise RuntimeError("Orchestrator did not preserve its consumed decision")
        outcomes.append({"query": query, "state": result["state"],
                         "next_action": decision["next_action"], "records": handles})
    catalog = engine.records.catalog()
    if catalog["verdict"] != "OK":
        raise RuntimeError("Record catalog reports damage")
    print(json.dumps({"outcomes": outcomes, "record_count": len(catalog["entries"]),
                      "record_root": str(engine.workspace / "organs"),
                      "scope": "Deterministic local peer reads, not independent semantic review"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

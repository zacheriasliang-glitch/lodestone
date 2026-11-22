"""Export durable example claims and actual callable exchanges into a handoff.

Run: python3 examples/machinery/review_handoff.py /tmp/lodestone-handoff
These deterministic providers demonstrate transport, not independent review.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery.inspection import export_nodes, pair_packet
from lodestone_machinery.nodes import NodeStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--omit-white", action="store_true")
    args = parser.parse_args()
    store = NodeStore(args.workspace)
    for identifier, text in (("observation", "Example repair retained the failed observation."),
                             ("interpretation", "The retained failure warrants examination.")):
        store.put({"id": identifier, "text": text, "layer": "AP", "status": "candidate",
                   "epoch": "example-1", "scope": "illustrative handoff",
                   "sources": ["bundled illustrative example; not experimental evidence"]})
    store.link("interpretation", "observation", "depends_on")
    board = export_nodes(store, ["observation", "interpretation"])

    def literal_reader(**request):
        return {"claims": [r["text"] for r in request["board"]["records"]],
                "counterpart": request["counterpart"], "position": "retain the literal records"}

    def boundary_reader(**request):
        return {"unverified_sources": [s for r in request["board"]["records"] for s in r["sources"]],
                "counterpart": request["counterpart"], "position": "source truth remains unchecked"}

    packet = pair_packet(board, literal_reader, None if args.omit_white else boundary_reader)
    # The route selects an actual content-bearing handoff, never an approval receipt.
    directory = args.workspace / ("pending-providers" if packet["gaps"] else "content-review")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "handoff.json"
    with target.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(packet, indent=2, ensure_ascii=False) + "\n")
    print(target.resolve())
    return 2 if packet["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())

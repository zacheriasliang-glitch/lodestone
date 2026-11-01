"""Write invented local inputs; never submit, approve or execute a task."""
import argparse
import hashlib
import json
from pathlib import Path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


RECEIPT = "receipts/20260101T000000Z__fixture__RCP-20260101T000000Z-0123456789ab.md"


def source_text(predecessors=()):
    document = {"receipt_id": "RCP-20260101T000000Z-0123456789ab",
                "operation_name": "fixture", "mode": "BOGUS",
                "receipt_directory_path_set_before_creation": list(predecessors)}
    return "# Fixture\n\n```json\n" + json.dumps(document) + "\n```\n"


def registry_text(state="HELD"):
    citation = "    cleared_by: fixture-review\n" if state == "CLEARED" else ""
    return ("# Captured fixture registry\n\n## BZ-0001\n\n    kind: SEED\n"
            "    law: Y2_ONE_ORGAN\n    subject: " + RECEIPT + "\n"
            "    first_seen: fixture\n    state: " + state + "\n" + citation +
            "    clears_when: a separate reviewed correction establishes a legal mode\n\n"
            "The captured fixture declares a mode outside the supported set.\n")


def file_record(text):
    return {"text": text, "sha256": digest(text.encode("utf-8"))}


def fixture():
    receipt = source_text()
    failure = {"schema_version": "0.2.0", "run_id": "fixture-failure", "status": "BLOCKED",
               "reason": "PROVIDER_UNDECLARED", "fold": None,
               "scope": {"sources": [{"path": RECEIPT, "sha256": digest(receipt.encode())}]},
               "authority": "NONE: output does not authorize actions"}
    return {"schema": "bezoar-reconciliation-source/v1", "corpus": "explicit-complete-fixture",
            "receipt_inventory": [RECEIPT],
            "files": {RECEIPT: file_record(receipt), "docs/bezoar.md": file_record(registry_text()),
                      "results/origami.json": file_record(json.dumps(failure))},
            "origami_results": ["results/origami.json"],
            "saved_lineage": {"verdict": "PASS", "findings": []}}


def write_sources(output):
    output = Path(output).expanduser().absolute()
    if output != output.resolve() or output.exists() or not output.parent.is_dir():
        raise ValueError("Choose a new output directory under an existing canonical parent")
    value = fixture()
    # The synthetic saved PASS intentionally conflicts with actual invalid mode;
    # the supported operation preserves that discrepancy, not a mock success.
    output.mkdir()
    def save(path, raw):
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    def encoded(v):
        return json.dumps(v, indent=2, sort_keys=True).encode() + b"\n"
    shelves = ["commons", "memory", "research", "workshop", "samsara"]
    save("profile.json", encoded({"schema": "nurarihyon-profile-source/v1",
        "baseline": {"board_name": "Lodestone Pando", "enabled_shelves": shelves},
        "candidate": {"board_name": "My local workshop", "enabled_shelves": shelves},
        "rationale": "Name this new synthetic workshop."}))
    save("document.md", b"# Workshop note\n\nKeep clay samples dry before weighing.\nRecord both observations when measurements disagree.\n")
    save("reconciliation.json", encoded(value))
    spec = {k:v for k,v in value.items() if k != "schema"}
    spec["files"] = {path: {"sha256": item["sha256"], "bytes": len(item["text"].encode())}
                     for path,item in value["files"].items()}
    for path,item in value["files"].items():
        save("corpus/" + path, item["text"].encode())
    save("capture-spec.json", encoded(spec))
    # Observation retains the same complete physical capture. The CLI can also
    # observe a corpus with missing historical references without claiming repair.
    save("observation-spec.json", encoded({**spec, "correction_evidence_paths": []}))
    save("README.txt", b"Invented test inputs only. No private history or model outputs.\nThe invalid receipt mode and stale PASS are deliberate discrepancy examples.\nNo task, approval, service, or model call is performed by this generator.\n")
    return {"status": "SYNTHETIC_INPUTS_PREPARED", "directory": str(output)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        print(json.dumps(write_sources(args.output)))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

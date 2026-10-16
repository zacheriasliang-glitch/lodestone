"""Organ-owned immutable evidence and exact peer reads for trusted local code.

Records live with their producer. The catalog contains pointers, not a second
evidence store. Hash checks establish byte identity, never semantic correctness,
reviewer independence, or permission to act.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
HANDLE = re.compile(r"([a-z][a-z0-9_-]{0,47}):([0-9a-f]{64})\Z")
MAX_BYTES = 16 * 1024 * 1024


class RecordUnavailable(ValueError):
    def __init__(self, handle, reason):
        self.handle = handle
        super().__init__(f"Record {handle} unavailable: {reason}")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class RecordStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._directory("organs", create=True)

    def _directory(self, relative, *, create=False):
        current = self.workspace
        for part in relative.split("/"):
            current = current / part
            if current.is_symlink():
                raise ValueError("Organ record path contains a symlink")
            if create:
                current.mkdir(exist_ok=True)
            if current.exists() and not current.is_dir():
                raise ValueError("Organ record directory is not a directory")
        return current

    def path(self, handle):
        match = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if match is None:
            raise ValueError("Invalid organ record handle")
        owner, identity = match.groups()
        directory = self._directory("organs/" + owner + "/records")
        path = directory / (identity + ".json")
        if path.is_symlink():
            raise ValueError("Organ record is a symlink")
        return path

    def publish(self, owner, kind, payload, *, links=()):
        if not isinstance(owner, str) or NAME.fullmatch(owner) is None:
            raise ValueError("Invalid record owner")
        if not isinstance(kind, str) or NAME.fullmatch(kind) is None or type(payload) is not dict:
            raise ValueError("Record requires a kind and object payload")
        if type(links) not in (tuple, list) or len(links) > 32:
            raise ValueError("Record supports at most 32 direct links")
        for handle in links:
            self.read(handle)
        if len(set(links)) != len(links):
            raise ValueError("Duplicate record link")
        record = {"schema": "organ-record/v1", "owner": owner, "kind": kind,
                  "payload": payload, "links": sorted(links)}
        data = canonical(record)
        if len(data) > MAX_BYTES:
            raise ValueError("Organ record exceeds size bound")
        handle = owner + ":" + hashlib.sha256(data).hexdigest()
        directory = self._directory("organs/" + owner + "/records", create=True)
        target = self.path(handle)
        # Publish complete bytes atomically without replacing an existing record.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".pending-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if self.read(handle) != record:
                    raise ValueError("Existing organ record differs")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return handle

    def read(self, handle):
        path = self.path(handle)
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != handle.split(":")[1]:
            raise ValueError("Organ record identity mismatch")
        record = json.loads(data)
        if (type(record) is not dict or set(record) != {"schema", "owner", "kind", "payload", "links"}
                or record["schema"] != "organ-record/v1" or record["owner"] != handle.split(":")[0]
                or not isinstance(record["kind"], str) or NAME.fullmatch(record["kind"]) is None
                or type(record["payload"]) is not dict or type(record["links"]) is not list
                or len(record["links"]) > 32
                or any(not isinstance(h, str) or HANDLE.fullmatch(h) is None for h in record["links"])
                or len(set(record["links"])) != len(record["links"]) or canonical(record) != data):
            raise ValueError("Invalid organ record schema")
        return record

    def relay(self, requester, handle, *, owner, kind):
        """A named consumer reads a specific producer contract; no access grant."""
        if not isinstance(requester, str) or NAME.fullmatch(requester) is None:
            raise ValueError("Invalid requesting role")
        record = self.read(handle)
        if record["owner"] != owner or record["kind"] != kind:
            raise ValueError("Producer or output contract mismatch")
        return record

    def verify_links(self, handles):
        pending, seen = list(handles), set()
        while pending:
            handle = pending.pop()
            if handle in seen:
                continue
            if len(seen) >= 256:
                raise ValueError("Record traversal exceeds 256 records")
            try:
                record = self.read(handle)
            except (OSError, ValueError) as exc:
                raise RecordUnavailable(handle, str(exc)) from exc
            seen.add(handle)
            pending.extend(record["links"])
        return {"status": "INTACT", "records_checked": len(seen),
                "scope": "Exact record bytes and reachable references only"}

    def catalog(self):
        entries = []
        for owner in sorted(self._directory("organs").iterdir()):
            if NAME.fullmatch(owner.name) is None:
                continue
            try:
                directory = self._directory("organs/" + owner.name + "/records")
                paths = sorted(directory.glob("*.json"))
                for path in paths:
                    handle = owner.name + ":" + path.stem
                    try:
                        record = self.read(handle)
                        self.verify_links([handle])
                        entries.append({"handle": handle, "owner": owner.name,
                                        "kind": record["kind"], "links": record["links"], "status": "INTACT"})
                    except (OSError, ValueError) as exc:
                        entries.append({"handle": handle, "owner": owner.name,
                                        "status": "DAMAGED", "reason": str(exc)})
            except (OSError, ValueError) as exc:
                entries.append({"owner": owner.name, "status": "UNAVAILABLE", "reason": str(exc)})
        return {"entries": entries, "verdict": "ATTENTION" if any(
            e["status"] != "INTACT" for e in entries) else "OK", "contains_payload_copies": False}


def route_result(store, audit_handle, measurement_handle, *, envelope_id, submission_handle):
    """Orchestrator consumes actual Auditor and Ruler records to choose state."""
    audit = store.relay("orchestrator", audit_handle, owner="auditor", kind="audit_result")
    measurement = store.relay("orchestrator", measurement_handle, owner="ruler", kind="measurement")
    store.relay("orchestrator", submission_handle, owner="builder", kind="worker_return")
    store.verify_links([audit_handle, measurement_handle])
    if (audit["payload"].get("envelope_id") != envelope_id
            or audit["payload"].get("submission") != submission_handle
            or submission_handle not in audit["links"]
            or measurement["payload"].get("envelope_id") != envelope_id
            or audit_handle not in measurement["links"]):
        raise ValueError("Record routing custody mismatch")
    verdict = audit["payload"].get("verdict")
    measured_output = measurement["payload"].get("measurement")
    measured = measured_output.get("verdict") if type(measured_output) is dict else None
    if verdict not in ("PASS", "FAIL", "UNKNOWN") or measured not in ("PASS", "FAIL", "UNKNOWN"):
        raise ValueError("Record routing requires explicit valid verdicts")
    state = ("BLOCKED" if verdict == "UNKNOWN" else
             "COMPLETED" if verdict == "PASS" and measured == "PASS" else "REJECTED")
    return {"state": state, "next_action": {"BLOCKED": "RETRIEVE_EVIDENCE",
            "COMPLETED": "RETURN_OUTPUT", "REJECTED": "REVISE_OUTPUT"}[state],
            "audit_verdict": verdict, "authority": "NONE"}

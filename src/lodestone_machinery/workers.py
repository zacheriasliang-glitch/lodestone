"""Explicit in-process worker extension points, with no implicit provider calls.

Registered Python workers are trusted local code, NOT a security sandbox. The
engine bounds its own writes and verifies return custody. A caller who needs
hostile-code isolation must supply it outside this process.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .organs import audit_brief, build_brief


@dataclass(frozen=True)
class WorkerRequest:
    envelope_id: str
    envelope: dict[str, Any]
    folded: dict[str, Any]


@dataclass(frozen=True)
class WorkerReturn:
    envelope_id: str
    artifact: str
    notes: str = ""


class Worker(Protocol):
    def build(self, request: WorkerRequest) -> WorkerReturn:
        """Produce useful work under the supplied immutable task identity."""
        ...


class Auditor(Protocol):
    def audit(self, root: Path, folded: dict[str, Any], artifact: str) -> dict[str, Any]:
        """Return PASS, FAIL, or UNKNOWN with findings, inspecting actual work."""
        ...


class LiteralExtractWorker:
    artifact_contract = "literal_brief"

    def build(self, request: WorkerRequest) -> WorkerReturn:
        return WorkerReturn(request.envelope_id, build_brief(request.folded),
                            "Deterministic literal source extracts; no semantic inference.")


class MechanicalAuditor:
    def audit(self, root: Path, folded: dict[str, Any], artifact: str) -> dict[str, Any]:
        return audit_brief(root, folded, artifact)

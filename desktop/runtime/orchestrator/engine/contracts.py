"""Typed contracts and validation for proposal-only orchestration decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypedDict


ProblemType = Literal[
    "IMPLEMENTATION", "AUDIT", "PROVENANCE", "RECOVERY", "ADMISSION", "PLANNING", "UNKNOWN"
]
Route = Literal["BUILDER", "AUDITOR", "HOST_APPROVAL", "STOP"]
Regime = Literal["CONTACT", "INFERENCE", "REGULATION", "RECALIBRATION", "DEVELOPMENT", "AUDIT"]

PROBLEM_TYPES = {
    "IMPLEMENTATION", "AUDIT", "PROVENANCE", "RECOVERY", "ADMISSION", "PLANNING", "UNKNOWN"
}
ROUTES = {"BUILDER", "AUDITOR", "HOST_APPROVAL", "STOP"}
REGIMES = {"CONTACT", "INFERENCE", "REGULATION", "RECALIBRATION", "DEVELOPMENT", "AUDIT"}


class ObservedContract(TypedDict):
    evidence: list[str]
    missing: list[str]
    contradictions: list[str]


class ProblemContract(TypedDict):
    primary: ProblemType
    rivals: list[ProblemType]
    evidence_used: list[str]
    minimal_falsifier: str


class MarrContract(TypedDict):
    computational: str
    algorithmic: list[str]
    implementation: str


class RegimeContract(TypedDict):
    regime: Regime
    entry_conditions: list[str]
    observed_evidence: list[str]
    confidence: float
    rival_regimes: list[Regime]
    minimal_falsifier: str
    protected_variables: list[str]
    action_authority: str
    exit_condition: str
    handoff_target: Route
    receipt_requirement: bool


class EquivalenceContract(TypedDict):
    equivalent: bool
    mismatches: list[str]
    falsifier: str


class BoundsContract(TypedDict):
    max_files: int
    max_receipts: int
    max_steps: int
    satisficing_threshold: float


class RivalRouteRejection(TypedDict):
    route: Route
    reason: str


class AuditFinding(TypedDict):
    schema_version: Literal["1.0.0"]
    producer_role: Literal["AUDITOR"]
    verdict: str
    load_bearing_gap: bool
    source_path: str
    source_byte_count: int
    source_sha256: str


class _OrchestrationDecisionOptional(TypedDict, total=False):
    audit_finding: AuditFinding


class OrchestrationDecision(_OrchestrationDecisionOptional):
    engine: Literal["K20_ORCHESTRATION_PHENOTYPE"]
    engine_version: Literal["1.0.0"]
    authority: Literal["PROPOSAL_ONLY"]
    decision_id: str
    experiment: str
    goal: str
    observed: ObservedContract
    problem: ProblemContract
    marr: MarrContract
    affordances: list[str]
    regime_contract: RegimeContract
    routing_layers: list[str]
    route_scores: dict[str, float]
    route: Route
    route_rationale: str
    rival_routes: list[Route]
    rival_route_rejections: list[RivalRouteRejection]
    equivalence_ledger: EquivalenceContract
    bounds: BoundsContract
    target_files: list[str]
    protected_files: list[str]
    predicted_outputs: list[str]
    predicted_protected_file_impact: str
    approval_required: bool
    stop_condition: str
    prediction_error_proxy: float
    uncertainty_proxy: float
    surprise_proxy: float
    confidence: float
    status: Literal["PROPOSED"]


@dataclass(frozen=True)
class Bounds:
    """Finite search limits with hard ceilings for local-machine safety."""

    max_files: int = 40
    max_receipts: int = 10
    max_steps: int = 8
    satisficing_threshold: float = 0.80

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "Bounds":
        raw = raw or {}
        bounds = cls(
            max_files=int(raw.get("max_files", raw.get("maxFiles", 40))),
            max_receipts=int(raw.get("max_receipts", raw.get("maxReceipts", 10))),
            max_steps=int(raw.get("max_steps", raw.get("maxSteps", 8))),
            satisficing_threshold=float(
                raw.get("satisficing_threshold", raw.get("satisficingThreshold", 0.80))
            ),
        )
        bounds.validate()
        return bounds

    def validate(self) -> None:
        limits = (
            ("max_files", self.max_files, 1, 200),
            ("max_receipts", self.max_receipts, 1, 50),
            ("max_steps", self.max_steps, 1, 32),
        )
        for name, value, minimum, maximum in limits:
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be within [{minimum}, {maximum}]")
        if not 0.0 <= self.satisficing_threshold <= 1.0:
            raise ValueError("satisficing_threshold must be within [0.0, 1.0]")

    def as_contract(self) -> BoundsContract:
        return {
            "max_files": self.max_files,
            "max_receipts": self.max_receipts,
            "max_steps": self.max_steps,
            "satisficing_threshold": self.satisficing_threshold,
        }


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_decision_id(decision: dict[str, Any]) -> str:
    canonical = copy.deepcopy(decision)
    canonical.pop("decision_id", None)
    return hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()


_FINDING_VERDICT_RE = re.compile(r"^[A-Z][A-Z_]*$")
_FINDING_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINDING_REQUIRED = {
    "schema_version", "producer_role", "verdict", "load_bearing_gap",
    "source_path", "source_byte_count", "source_sha256",
}


def _validate_audit_finding_dict(finding: Any) -> None:
    """Strict semantic validation of an optional typed Auditor finding.

    Fails closed. Never mutates. Applies only when the key is present, so
    historical decisions are unaffected.
    """

    if not isinstance(finding, dict):
        raise ValueError("audit_finding must be an object")
    missing = sorted(_FINDING_REQUIRED - finding.keys())
    if missing:
        raise ValueError(f"audit_finding missing required fields: {', '.join(missing)}")
    extra = sorted(finding.keys() - _FINDING_REQUIRED)
    if extra:
        raise ValueError(f"audit_finding has unexpected fields: {', '.join(extra)}")
    if finding["schema_version"] != "1.0.0":
        raise ValueError("audit_finding schema_version must be 1.0.0")
    if finding["producer_role"] != "AUDITOR":
        raise ValueError("audit_finding producer_role must be AUDITOR")
    if not isinstance(finding["verdict"], str) or not _FINDING_VERDICT_RE.fullmatch(
        finding["verdict"]
    ):
        raise ValueError("audit_finding verdict must be an uppercase token")
    if type(finding["load_bearing_gap"]) is not bool:
        raise ValueError("audit_finding load_bearing_gap must be exactly bool")
    if not isinstance(finding["source_path"], str) or not finding["source_path"].strip():
        raise ValueError("audit_finding source_path must be a non-empty string")
    if isinstance(finding["source_byte_count"], bool) or not isinstance(
        finding["source_byte_count"], int
    ):
        raise ValueError("audit_finding source_byte_count must be an integer")
    if finding["source_byte_count"] < 0:
        raise ValueError("audit_finding source_byte_count must be non-negative")
    if not isinstance(finding["source_sha256"], str) or not _FINDING_SHA256_RE.fullmatch(
        finding["source_sha256"]
    ):
        raise ValueError(
            "audit_finding source_sha256 must be a 64-character lowercase hex string"
        )


def validate_decision(decision: dict[str, Any]) -> None:
    required = {
        "engine", "engine_version", "authority", "decision_id", "experiment", "goal", "observed",
        "problem", "marr", "affordances", "regime_contract", "routing_layers", "route_scores", "route",
        "route_rationale", "rival_routes", "rival_route_rejections", "equivalence_ledger", "bounds",
        "target_files", "protected_files", "predicted_outputs", "predicted_protected_file_impact",
        "approval_required", "stop_condition", "prediction_error_proxy", "uncertainty_proxy",
        "surprise_proxy", "confidence", "status",
    }
    missing = sorted(required - decision.keys())
    if missing:
        raise ValueError(f"decision missing required fields: {', '.join(missing)}")
    if decision["engine"] != "K20_ORCHESTRATION_PHENOTYPE":
        raise ValueError("unexpected engine identifier")
    if decision["engine_version"] != "1.0.0" or decision["authority"] != "PROPOSAL_ONLY":
        raise ValueError("unexpected engine version or authority")
    if not re.fullmatch(r"\d{3}", decision["experiment"]):
        raise ValueError("experiment must be a three-digit string")
    if decision["problem"]["primary"] not in PROBLEM_TYPES:
        raise ValueError("invalid problem type")
    if decision["route"] not in ROUTES:
        raise ValueError("invalid route")
    if decision["regime_contract"]["regime"] not in REGIMES:
        raise ValueError("invalid regime")
    if set(decision["route_scores"]) != ROUTES:
        raise ValueError("route_scores must contain every allowed route exactly once")
    for name, value in decision["route_scores"].items():
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"invalid route score for {name}")
    for name in ("prediction_error_proxy", "uncertainty_proxy", "surprise_proxy", "confidence"):
        value = decision[name]
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"{name} must be within [0.0, 1.0]")
    if not decision["problem"]["minimal_falsifier"].strip():
        raise ValueError("problem minimal_falsifier must be non-empty")
    if not decision["regime_contract"]["minimal_falsifier"].strip():
        raise ValueError("regime minimal_falsifier must be non-empty")
    if not decision["equivalence_ledger"]["falsifier"].strip():
        raise ValueError("equivalence falsifier must be non-empty")
    if decision["authority"] == "PROPOSAL_ONLY" and decision["status"] != "PROPOSED":
        raise ValueError("proposal-only decisions must remain PROPOSED")
    Bounds.from_mapping(decision["bounds"])
    if "audit_finding" in decision:
        _validate_audit_finding_dict(decision["audit_finding"])
    expected_id = compute_decision_id(decision)
    if decision["decision_id"] != expected_id:
        raise ValueError("decision_id does not match canonical decision content")


def validate_json_schema(instance: Any, schema: dict[str, Any], path: str = "root") -> None:
    """Validate the JSON-Schema subset used by the local decision contract."""

    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "number": (int, float),
        "integer": int,
    }
    if expected in type_map:
        if expected in {"number", "integer"} and isinstance(instance, bool):
            raise ValueError(f"{path}: expected {expected}")
        if not isinstance(instance, type_map[expected]):
            raise ValueError(f"{path}: expected {expected}")
    if "const" in schema and instance != schema["const"]:
        raise ValueError(f"{path}: value does not match const")
    if "enum" in schema and instance not in schema["enum"]:
        raise ValueError(f"{path}: value is outside enum")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            raise ValueError(f"{path}: string is too short")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise ValueError(f"{path}: string does not match pattern")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValueError(f"{path}: value is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise ValueError(f"{path}: value is above maximum")
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            validate_json_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                raise ValueError(f"{path}: missing required property {key}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instance) - set(properties))
            if extras:
                raise ValueError(f"{path}: unexpected properties {', '.join(extras)}")
        for key, value in instance.items():
            if key in properties:
                validate_json_schema(value, properties[key], f"{path}.{key}")

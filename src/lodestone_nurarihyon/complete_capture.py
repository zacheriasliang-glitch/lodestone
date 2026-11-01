"""Explicit captured-evidence contracts; disabled by existing host policies."""
from lodestone_machinery import bezoar_capture as capture
from .common import canonical, digest
from .plans import PlanError, _current, _typed_equal

TOOLKIT = "bezoar.reconciliation-capture/v1"
PLAN_SCHEMA = "nurarihyon-complete-capture-plan/v1"
OBSERVATION_TOOLKIT = "bezoar.reconciliation-observation/v1"
OBSERVATION_PLAN_SCHEMA = "nurarihyon-reconciliation-observation-plan/v1"
CAPTURE_TOOLKITS = (TOOLKIT, OBSERVATION_TOOLKIT)


def capture_module(toolkit):
    if toolkit == TOOLKIT:
        return capture
    if toolkit == OBSERVATION_TOOLKIT:
        from lodestone_machinery import bezoar_observation
        return bezoar_observation
    raise PlanError("REJECTED_VALIDATION", "Unknown captured-evidence contract")


def make_reconciliation_plan(raw, current_view):
    return _make_plan(raw, current_view, TOOLKIT)


def make_observation_plan(raw, current_view):
    return _make_plan(raw, current_view, OBSERVATION_TOOLKIT)


def _make_plan(raw, current_view, toolkit):
    value = capture_module(toolkit).manifest(raw, digest(raw))
    observation = toolkit == OBSERVATION_TOOLKIT
    plan = {"schema": OBSERVATION_PLAN_SCHEMA if observation else PLAN_SCHEMA, "toolkit": toolkit,
            "source_sha256": digest(raw), "source_bytes": len(raw),
            "corpus": value["corpus"], "receipt_inventory": value["receipt_inventory"],
            "captured_files": [{"path": name, "bytes": item["bytes"], "sha256": item["sha256"]}
                               for name, item in sorted(value["files"].items())],
            "origami_results": value["origami_results"], "groups": value["groups"],
            "baseline": _current(current_view), "authority": "PROPOSAL_ONLY",
            "operation": "historical-declaration-observation/v1" if observation else
                         "complete-captured-lineage-and-registry-coverage/v1", "activation": "NONE",
            "closure": "Entire declared captured corpus; unseen repositories are outside this operation",
            "original_outcomes": "RETAIN_UNCHANGED", "repair": "NONE", "learning": False}
    if observation:
        plan["closure"] = "Complete physical capture and Origami bindings; historical absence remains unresolved"
        plan["correction_evidence_paths"] = value["correction_evidence_paths"]
        plan["corrections_applied"] = False
    return plan


def validate_reconciliation_plan(plan, raw, current_view):
    _validate_plan(plan, raw, current_view, TOOLKIT)


def validate_observation_plan(plan, raw, current_view):
    _validate_plan(plan, raw, current_view, OBSERVATION_TOOLKIT)


def _validate_plan(plan, raw, current_view, toolkit):
    if not _typed_equal(plan, _make_plan(raw, current_view, toolkit)):
        raise PlanError("REJECTED_VALIDATION", "Captured-evidence plan differs from exact input and contract")


def validate_route_selection(plan, board, raw):
    if "retained_routing" in plan:
        raise PlanError("REJECTED_VALIDATION", "Retained routing is not part of this captured-evidence contract")


def build(board, raw, task, *, bundle, scratch):
    from .reconciliation import build_reconciliation
    return build_reconciliation(board, raw, task, scratch=scratch, capture_bundle=bundle)


def validate_stage(board, parent, raw, task, plan, report, policy, *, stage, bundle):
    from .reconciliation import validate_reconciliation_stage
    return validate_reconciliation_stage(board, parent, raw, task, plan, report, policy,
                                          stage=stage, capture_bundle=bundle)

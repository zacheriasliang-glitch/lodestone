"""Check a pinned confirmation report and apply its frozen score thresholds.

These are provenance/count gates, not a rule-based semantic substitute. Only
the learned entailment score determines a support decision. Caller-supplied
pins do not authenticate a reviewer or establish general deployment reliability.
"""
import hashlib
import json
import math
import re

from . import loom_nli
from .records import canonical

GATES = {"zero_accepted_errors", "zero_false_support", "positive_coverage_at_least_80_percent",
         "overall_coverage_at_least_80_percent", "forced_binary_accuracy_at_least_90_percent",
         "conditional_pairs_without_forced_miss", "pins_runtime_and_determinism_verified"}
COUNTS = ("total", "gold_supports", "gold_nonsupports", "accepted", "accepted_supports", "true_supports",
          "false_supports", "wrong_negatives", "accepted_errors", "forced_correct",
          "forced_false_supports", "forced_wrong_negatives", "refusals")
RUNTIME_PATH = "code-snapshots/loom_nli-confirmation.py"
CLAIM_ONLY_PREMISE = "No premise has been supplied."
CONSUMER_ADMISSION_RULE = "claim-only-original-key-margin/v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pin(value):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "Invalid SHA-256 pin")
    return value


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rate(value, numerator, denominator):
    require(type(value) is dict and {"numerator", "denominator", "rate"} <= set(value), "Missing count/rate")
    require(type(value["numerator"]) is int and type(value["denominator"]) is int
            and value["numerator"] == numerator and value["denominator"] == denominator, "Inconsistent rate counts")
    expected = numerator / denominator if denominator else None
    require(value["rate"] is None if expected is None else finite(value["rate"])
            and math.isclose(value["rate"], expected, rel_tol=0, abs_tol=1e-12), "Inconsistent reported rate")


def _metrics(value, total):
    require(type(value) is dict, "Confirmation metrics required")
    require(all(type(value.get(k)) is int and 0 <= value[k] <= total for k in COUNTS), "Invalid metric count")
    require(value["total"] == total and value["gold_supports"] == total // 3
            and value["gold_nonsupports"] == total * 2 // 3, "Wrong confirmation denominators")
    confusion = value.get("confusion")
    golds, decisions = {"SUPPORTED", "CONTRADICTED", "INSUFFICIENT"}, {"SUPPORTED", "NOT_SUPPORTED", "ABSTAIN"}
    require(type(confusion) is dict and set(confusion) == golds, "Missing three-label confusion counts")
    for row in confusion.values():
        require(type(row) is dict and set(row) == decisions
                and all(type(n) is int and 0 <= n <= total // 3 for n in row.values())
                and sum(row.values()) == total // 3, "Inconsistent confusion row")
    accepted_supports = sum(row["SUPPORTED"] for row in confusion.values())
    true_supports = confusion["SUPPORTED"]["SUPPORTED"]
    false_supports = accepted_supports - true_supports
    wrong_negatives = confusion["SUPPORTED"]["NOT_SUPPORTED"]
    accepted = total - sum(row["ABSTAIN"] for row in confusion.values())
    expected = {"accepted_supports": accepted_supports, "true_supports": true_supports,
                "false_supports": false_supports, "wrong_negatives": wrong_negatives,
                "accepted_errors": false_supports + wrong_negatives, "accepted": accepted}
    require(all(value[k] == v for k, v in expected.items()), "Metrics disagree with confusion counts")
    require(value["refusals"] <= total - accepted
            and value["forced_correct"] + value["forced_false_supports"]
                + value["forced_wrong_negatives"] + value["refusals"] == total
            and value["forced_correct"] >= accepted - value["accepted_errors"]
            and false_supports <= value["forced_false_supports"] <= value["gold_nonsupports"]
            and wrong_negatives <= value["forced_wrong_negatives"] <= value["gold_supports"],
            "Inconsistent forced/accepted counts")
    require(type(value.get("supported_misses")) is int
            and value["supported_misses"] == value["gold_supports"] - true_supports, "Inconsistent supported misses")
    for key, numerator, denominator in (
            ("coverage", accepted, total), ("positive_coverage", true_supports, total // 3),
            ("forced_binary_accuracy", value["forced_correct"], total),
            ("selective_accuracy", accepted - value["accepted_errors"], accepted),
            ("false_support_among_accepted_supports", false_supports, accepted_supports),
            ("false_support_among_gold_nonsupports", false_supports, total * 2 // 3)):
        _rate(value.get(key), numerator, denominator)
    return {key: value[key] for key in COUNTS}


def _claim_only_admission(report, freeze):
    """Pre-unblinding consumer requirement; the frozen scorer is unchanged."""
    diagnostic = report.get("claim_only_diagnostic")
    require(type(diagnostic) is dict, "Claim-only diagnostic is required")
    control = {"text": CLAIM_ONLY_PREMISE,
               "sha256": hashlib.sha256(CLAIM_ONLY_PREMISE.encode("utf-8")).hexdigest()}
    require(freeze.get("claim_only_premise") == control and diagnostic.get("control_premise") == control,
            "Claim-only control differs from the frozen literal")
    matched = diagnostic.get("forced_answer_key_match")
    require(type(matched) is dict and type(matched.get("numerator")) is int
            and 0 <= matched["numerator"] <= 180, "Invalid claim-only original-key count")
    count = matched["numerator"]
    _rate(matched, count, 180)
    _rate(diagnostic.get("majority_answer_key_match"), 120, 180)
    excess = (count - 120) / 180
    exceeds = count - 120 > 18
    require(finite(diagnostic.get("excess_over_majority"))
            and math.isclose(diagnostic["excess_over_majority"], excess, rel_tol=0, abs_tol=1e-12)
            and diagnostic.get("exceeds_majority_by_more_than_0_10") is exceeds,
            "Claim-only excess/flag differs from the fixed counts")
    require(type(diagnostic.get("changed_forced_choices")) is int
            and 0 <= diagnostic["changed_forced_choices"] <= 180, "Invalid claim-only changed-choice count")
    require(not exceeds, "Consumer admission refuses claim-only original-key match above majority plus 0.10")
    return json.loads(canonical(diagnostic))


def validate_confirmation(report, freeze, *, freeze_sha256, manifest, manifest_sha256, runtime_sha256):
    """Validate the current 60-family/180-row protocol without opening its data."""
    pin(freeze_sha256), pin(manifest_sha256), pin(runtime_sha256)
    require(type(report) is dict and report.get("schema") == "learned-confirmation-score/v1"
            and report.get("status") == "NECESSARY_GATES_PASS", "A passing pinned confirmation report is required")
    require(type(freeze) is dict and freeze.get("schema") == "learned-confirmation-freeze/v1"
            and report.get("run_freeze_sha256") == freeze_sha256, "Confirmation belongs to another run freeze")
    require(type(report.get("gates")) is dict and set(report["gates"]) == GATES
            and all(value is True for value in report["gates"].values()), "Every named confirmation gate must pass")
    overall = _metrics(report.get("overall"), 180)
    families = report.get("families")
    require(type(families) is dict and len(families) == 60, "Exactly 60 confirmation families required")
    family_counts = [_metrics(value, 3) for value in families.values()]
    require(all(sum(row[key] for row in family_counts) == overall[key] for key in COUNTS),
            "Family counts disagree with overall counts")
    require(overall["accepted_errors"] == overall["false_supports"] == overall["wrong_negatives"] == 0
            and overall["accepted"] * 5 >= 180 * 4 and overall["true_supports"] * 5 >= 60 * 4
            and overall["forced_correct"] * 10 >= 180 * 9, "Necessary coverage/error/accuracy counts fail")
    require(report.get("conditional_family_review_required") == [], "Conditional family failures remain")
    phenomena = report.get("phenomena")
    conditions = phenomena.get("conditions") if type(phenomena) is dict else None
    require(type(conditions) is dict and type(conditions.get("family_count")) is int
            and 1 <= conditions["family_count"] <= 60, "At least one explicit conditions family is required")
    condition_counts = _metrics(conditions, conditions["family_count"] * 3)
    require(all(condition_counts[key] <= overall[key] for key in COUNTS),
            "Conditions metrics exceed the overall counts")
    claim_only = _claim_only_admission(report, freeze)
    determinism = report.get("determinism", {})
    require(determinism.get("exact_logits") is True and type(determinism.get("checked_repeat_count")) is int
            and determinism["checked_repeat_count"] >= 1, "Verified determinism is required")
    bounds = report.get("one_sided_95_percent_exact_upper_error_bounds", {})
    _rate(bounds.get("families_with_any_accepted_error"), 0, 60)
    _rate(bounds.get("accepted_prediction_error"), 0, overall["accepted"])
    threshold, negative = freeze.get("support_threshold"), freeze.get("negative_threshold")
    require(finite(threshold) and threshold in {0.8, 0.9, 0.95, 0.975, 0.99, 0.995}
            and finite(negative) and negative == 0.1 and report.get("support_threshold") == threshold
            and report.get("negative_threshold") == negative, "Thresholds differ from the frozen protocol")
    candidate = loom_nli._candidate(manifest)
    _, computed_manifest_sha = loom_nli._manifest(manifest)
    require(computed_manifest_sha == manifest_sha256, "Model manifest canonical pin differs")
    model = freeze.get("model")
    require(type(model) is dict and canonical(report.get("model")) == canonical(model), "Report model differs from freeze")
    expected = {"repository": candidate["repository"], "revision": candidate["revision"],
                "weights_sha256": candidate["weights_sha256"], "manifest_sha256": manifest_sha256,
                "parameter_count": candidate["float_elements"], "device": "cpu", "dtype": "float32",
                "storage_dtype": candidate["storage_dtype"], "labels": candidate["labels"],
                "threads": 2, "interop_threads": 1, "int64_elements": candidate["int64_elements"],
                "float32_elements" if candidate["storage_dtype"] == "F32" else "float16_elements": candidate["float_elements"]}
    require(all(type(model.get(k)) is type(v) and model[k] == v for k, v in expected.items())
            and freeze.get("label_names") == candidate["labels"], "Model identity differs from pinned assets/runtime")
    require(set(model) == set(expected) | {"tensor_count"} and type(model["tensor_count"]) is int
            and 1 <= model["tensor_count"] <= 10000, "Unsupported complete model identity")
    files = freeze.get("files")
    require(type(files) is list and files and all(type(row) is dict and set(row) == {"path", "sha256"}
            and type(row["path"]) is str for row in files), "Frozen file identities required")
    require(len({row["path"] for row in files}) == len(files), "Duplicate frozen path")
    by_path = {row["path"]: pin(row["sha256"]) for row in files}
    require(by_path.get(RUNTIME_PATH) == runtime_sha256, "Current NLI runtime differs from confirmed runtime")
    require({manifest_sha256, candidate["weights_sha256"]} <= set(by_path.values()), "Model pins absent from run freeze")
    binding = {key: pin(report.get(key)) for key in
               ("run_freeze_sha256", "result_freeze_sha256", "result_sha256", "receipt_sha256")}
    require(type(freeze.get("evaluation_receipt")) is dict
            and binding["receipt_sha256"] == freeze["evaluation_receipt"].get("sha256"), "Evaluation receipt pin differs")
    return {"support_threshold": threshold, "negative_threshold": negative,
            "model": model, "labels": candidate["labels"], "runtime_sha256": runtime_sha256,
            "confirmation_binding": binding, "confirmation_status": "NECESSARY_GATES_PASS",
            "claim_only_diagnostic": claim_only,
            "consumer_admission": {"rule": CONSUMER_ADMISSION_RULE,
                                   "specified_date": "2026-11-11 Asia/Singapore",
                                   "stage": "PRE_CONFIRMATION_UNBLINDING_CONSUMER_REQUIREMENT",
                                   "maximum_original_key_excess_over_majority": 0.10,
                                   "status": "PASS", "frozen_scorer_gates_changed": False,
                                   "model_threshold_changed": False},
            "scope": "PASSAGE_SUPPORT_NOT_WORLD_TRUTH"}


def decide(score, policy, prepared):
    """Apply only frozen thresholds to validated learned logits/probabilities."""
    labels = policy["labels"]
    require(type(score) is dict and score.get("labels") == labels, "Scorer label mapping differs")
    probabilities, supported, nonsupported = loom_nli._probabilities(score.get("logits"), labels)
    require(type(score.get("probabilities")) is list and len(score["probabilities"]) == len(labels)
            and all(finite(a) and math.isclose(a, b, rel_tol=0, abs_tol=1e-12)
                    for a, b in zip(score["probabilities"], probabilities)), "Scorer probabilities differ from logits")
    require(finite(score.get("p_supported")) and finite(score.get("p_not_supported"))
            and math.isclose(score["p_supported"], supported, rel_tol=0, abs_tol=1e-12)
            and math.isclose(score["p_not_supported"], nonsupported, rel_tol=0, abs_tol=1e-12),
            "Scorer support probability differs from logits")
    for name in ("premise", "hypothesis"):
        for suffix in ("sha256", "bytes"):
            key = name + "_" + suffix
            require(type(score.get(key)) is type(prepared["provenance"][key])
                    and score[key] == prepared["provenance"][key], "Scorer input identity differs")
    require(type(score.get("token_count")) is int and 1 <= score["token_count"] <= 512,
            "Complete input token count is invalid")
    return ("SUPPORTED" if supported >= policy["support_threshold"] else
            "NOT_SUPPORTED" if supported <= policy["negative_threshold"] else "ABSTAIN")

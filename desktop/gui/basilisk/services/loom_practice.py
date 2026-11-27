"""Per-encounter GUI options, with explicit source binding and ordinary omission."""
from copy import deepcopy
import hashlib
import json
import re

SCHEMA = "loom-gui-practice-options/v1"


def _sha(text):
	return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_options(value):
	if value is None:
		return None
	fields = {"schema", "encounter_sha256", "purpose_sha256", "seed_handles", "history_window",
		"decision_case_json", "decision_strategy"}
	if type(value) is not dict or set(value) not in (fields, fields | {"decision_knot_handles"}) or value["schema"] != SCHEMA:
		raise ValueError("Unsupported saved practice options")
	knots = value.get("decision_knot_handles")
	if knots is not None and (type(knots) is not list or not 1 <= len(knots) <= 16
		or any(type(h) is not str or re.fullmatch(r"bezoar:[0-9a-f]{64}", h) is None for h in knots)
		or len(set(knots)) != len(knots)):
		raise ValueError("Choose 1–16 distinct machine finding handles, or leave blank")
	for field in ("encounter_sha256", "purpose_sha256"):
		if type(value[field]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None:
			raise ValueError("Invalid practice encounter binding")
	seeds = value["seed_handles"]
	if seeds is not None and (type(seeds) is not list or not 1 <= len(seeds) <= 16
		or any(type(h) is not str or re.fullmatch(r"bezoar:[0-9a-f]{64}", h) is None for h in seeds)
		or len(set(seeds)) != len(seeds)):
		raise ValueError("Choose 1–16 distinct retained lessons, or use the configured selection")
	window = value["history_window"]
	if window is not None and (type(window) is not int or not 1 <= window <= 32):
		raise ValueError("History window must be 1–32 runs, or automatic")
	case = value["decision_case_json"]
	if case is not None:
		if type(case) is not str or not case.strip() or len(case.encode()) > 8192:
			raise ValueError("Case JSON must be nonempty and at most 8192 bytes")
		if type(json.loads(case)) is not dict:
			raise ValueError("Case JSON must be an object")
	if value["decision_strategy"] not in ("flat", "evidence_first"):
		raise ValueError("Unsupported decision response strategy")
	if value["decision_strategy"] == "evidence_first" and case is None:
		raise ValueError("Evidence-first responses require the exact case JSON")
	return deepcopy(value)


def bind_options(encounter, purpose, *, seed_handles=None, history_window=None,
		decision_case_json=None, decision_strategy="flat", decision_knot_handles=None):
	if seed_handles is None and history_window is None and decision_case_json is None and decision_strategy == "flat" and decision_knot_handles is None:
		return None
	value = validate_options({"schema": SCHEMA, "encounter_sha256": _sha(encounter),
		"purpose_sha256": _sha(purpose.strip()), "seed_handles": seed_handles, "history_window": history_window,
		"decision_case_json": decision_case_json, "decision_strategy": decision_strategy})
	if decision_knot_handles is not None:
		value = validate_options(dict(value, decision_knot_handles=decision_knot_handles))
	if decision_case_json is not None and encounter.count(decision_case_json) != 1:
		raise ValueError("Case JSON must be one exact, unique span of the current message")
	return value


def bridge_options(value, encounter, purpose):
	value = validate_options(value)
	if value is None:
		return {}
	if value["encounter_sha256"] != _sha(encounter) or value["purpose_sha256"] != _sha(purpose.strip()):
		raise ValueError("The message or purpose changed. Review Practice options for this encounter, or clear them.")
	case = value["decision_case_json"]
	if case is not None and encounter.count(case) != 1:
		raise ValueError("The selected case JSON is no longer an exact, unique span of this message")
	result = {key: deepcopy(value[key]) for key in ("seed_handles", "history_window", "decision_case_json")
		if value[key] is not None}
	if value["decision_strategy"] != "flat":
		result["decision_strategy"] = value["decision_strategy"]
	if value.get("decision_knot_handles") is not None:
		result["decision_knot_handles"] = deepcopy(value["decision_knot_handles"])
	return result


def summary(value, encounter, purpose):
	if value is None:
		return "Ordinary defaults"
	try:
		options = bridge_options(value, encounter, purpose)
	except ValueError:
		return "Saved options need review for this message"
	parts = []
	if "seed_handles" in options:
		parts.append(f"{len(options['seed_handles'])} selected lesson(s)")
	if "history_window" in options:
		parts.append(f"latest {options['history_window']} practice runs")
	if "decision_case_json" in options:
		parts.append("evidence-first case response" if options.get("decision_strategy") == "evidence_first" else "structured case response")
	if "decision_knot_handles" in options:
		parts.append(f"{len(options['decision_knot_handles'])} machine finding(s) as named knots")
	return "; ".join(parts)

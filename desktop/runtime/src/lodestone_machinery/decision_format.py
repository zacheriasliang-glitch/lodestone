"""Source-bound optional decoding shape, never a supplied decision or grader key.

All declared actions and observation IDs remain available, including unverified,
older and other-target observations. The model must choose the action, transition
and evidence. This module neither interprets conditions nor calls a provider.
"""
from .decision_contract import (
    MAX_CONTRACT_BYTES, _canonical, _case, _copy_json, _require, _sha,
    bind_decision_contract,
)


SCHEMA = "lodestone-decision-format/v1"
SOURCE_AUTHOR = "Loom explicit structured decision decoding"
MAX_FORMAT_BYTES = 65536


def bind_decision_format(encounter, *, case_json):
    """Bind an exact unique case span and retain every supplied choice.

    No typed condition policy is accepted. Source validation reuses the same
    bounded strict case schema as decision review, without deriving an answer.
    An empty observation list is rejected: an exactly-one citation schema cannot
    offer a source-backed choice in that case.
    """
    source = bind_decision_contract(encounter, case_json=case_json,
                                    author=SOURCE_AUTHOR)
    case = _case(source["case_json"])
    ids = [row["id"] for row in case["observations"]]
    _require(bool(ids), "Structured decision decoding requires an observation ID")
    response_schema = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "const": case["case_id"]},
            "action": {"type": "string", "enum": case["allowed_actions"]},
            "transition": {"type": "string", "enum": ["HOLD", "CHANGE"]},
            "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 1,
                             "items": {"type": "string", "enum": ids}},
        },
        "required": ["case_id", "action", "transition", "evidence_ids"],
        "additionalProperties": False,
    }
    result = {
        "schema": SCHEMA, "source_contract": source,
        "response_schema": response_schema,
        "response_schema_sha256": _sha(_canonical(response_schema)),
        "choice_scope": "ALL_DECLARED_ACTIONS_AND_OBSERVATION_IDS_BOTH_TRANSITIONS",
        "authority": "NONE", "semantic_learning": "NOT_ESTABLISHED",
    }
    result["format_sha256"] = _sha(_canonical(result))
    return _copy_json(result, MAX_FORMAT_BYTES)


def validate_decision_format(encounter, value):
    """Return a detached binding only if exact source and entire schema match."""
    saved = _copy_json(value, MAX_FORMAT_BYTES)
    _require(type(saved) is dict and type(saved.get("source_contract")) is dict,
             "Expected a source-bound decision format")
    source = _copy_json(saved["source_contract"], MAX_CONTRACT_BYTES)
    _require(type(source.get("case_json")) is str, "Decision format lacks its exact case span")
    expected = bind_decision_format(encounter, case_json=source["case_json"])
    _require(_canonical(saved) == _canonical(expected),
             "Decision format differs from its exact encounter or full choice set")
    return expected
